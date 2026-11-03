import csv
import time
import os
import sys
import re
import ctypes
import subprocess
from datetime import datetime
import psutil
import wmi
import win32pdh
from PyQt6.QtWidgets import (QApplication, QMainWindow, QTextEdit, QPushButton,
                             QVBoxLayout, QWidget, QLabel)
from PyQt6.QtCore import QThread, pyqtSignal

_HAS_LH = False
_LH_COMPUTER = None
_LH_CPU_HW = None
_LH_SENSORS = {}


def _is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _elevate():
    if _is_admin():
        return
    try:
        params = " ".join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit(0)
    except Exception:
        pass


def _ensure_lh_driver(log_cb=None):
    def _log(msg):
        if log_cb:
            log_cb(msg)
    base = _get_base_dir()
    if getattr(sys, "frozen", False):
        candidates = [
            os.path.join(base, "LibreHardwareMonitor", "LibreHardwareMonitor.exe"),
            os.path.join(base, "_internal", "LibreHardwareMonitor", "LibreHardwareMonitor.exe"),
        ]
    else:
        candidates = [os.path.join(base, "LibreHardwareMonitor", "LibreHardwareMonitor.exe")]
    lh_exe = next((p for p in candidates if os.path.isfile(p)), None)
    if lh_exe is None:
        _log("⚠️  找不到 LibreHardwareMonitor.exe，无法自动安装驱动")
        return False
    _log("🔧 正在后台启动 LibreHardwareMonitor 以自动安装驱动...")
    try:
        proc = subprocess.Popen(
            [lh_exe],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        try:
            proc = subprocess.Popen([lh_exe])
        except Exception as e:
            _log(f"❌ 启动 LibreHardwareMonitor.exe 失败: {e}")
            return False
    _log("   等待 6 秒让驱动完成安装...")
    time.sleep(6)
    try:
        proc.kill()
    except Exception:
        pass
    time.sleep(1)
    _log("✅ 驱动安装完成（或已存在）")
    return True


def _find_temp_sensor(h, name):
    for s in h.Sensors:
        if s.SensorType.ToString() == "Temperature" and s.Name == name:
            return s
    for sub in h.SubHardware:
        r = _find_temp_sensor(sub, name)
        if r is not None:
            return r
    return None


def _get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_log_dir():
    return _get_base_dir()


def _init_librehwmonitor(log_cb=None, _retried=False):
    global _HAS_LH, _LH_COMPUTER, _LH_CPU_HW, _LH_SENSORS
    def _log(msg):
        if log_cb:
            log_cb(msg)
    base = _get_base_dir()
    candidates = [
        os.path.join(base, "LibreHardwareMonitor"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "LibreHardwareMonitor"),
    ]
    if getattr(sys, "frozen", False):
        candidates.insert(1, os.path.join(base, "_internal", "LibreHardwareMonitor"))
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.insert(0, os.path.join(meipass, "LibreHardwareMonitor"))
    dll_dir = next((p for p in candidates if os.path.isdir(p)), None)
    if dll_dir is None:
        _log(f"❌ 未找到 LibreHardwareMonitor 目录 (candidates={candidates})")
        return False
    _log(f"📁 LibreHardwareMonitor 目录: {dll_dir}")
    if not _is_admin():
        _log("⚠️  当前非管理员权限，温度可能无法读取（需要管理员权限）")
    try:
        sys.path.insert(0, dll_dir)
        try:
            import clr
        except Exception as e:
            _log(f"❌ import clr 失败 (缺少 .NET Runtime?): {e}")
            return False
        try:
            clr.AddReference("LibreHardwareMonitorLib")
        except Exception as e:
            _log(f"❌ clr.AddReference('LibreHardwareMonitorLib') 失败: {e}")
            return False
        from LibreHardwareMonitor import Hardware
        computer = Hardware.Computer()
        computer.IsCpuEnabled = True
        try:
            computer.Open()
        except Exception as e:
            _log(f"❌ computer.Open() 失败: {e}")
            return False
        cpu_hw = None
        for hw in computer.Hardware:
            if hw.HardwareType == Hardware.HardwareType.Cpu:
                cpu_hw = hw
                break
        if cpu_hw is None:
            _log("❌ 未检测到 CPU 硬件")
            computer.Close()
            return False
        cpu_hw.Update()
        mapping = {
            "pkg": "CPU Package",
            "max": "Core Max",
            "avg": "Core Average",
        }
        found = {}
        none_count = 0
        for key, name in mapping.items():
            s = _find_temp_sensor(cpu_hw, name)
            if s is not None:
                found[key] = s
                try:
                    v = s.Value
                    if v is None:
                        none_count += 1
                    _log(f"  📡 {name:30s} → Value={v}")
                except Exception as e:
                    _log(f"  📡 {name:30s} → 读取异常: {e}")
        if "pkg" not in found:
            _log("❌ 未找到 'CPU Package' 温度传感器")
            computer.Close()
            return False
        if none_count >= 3:
            _log("⚠️  三个温度传感器 Value 全为 None → Ring0 驱动未安装！")
            if not _retried:
                computer.Close()
                _log("🔄 首次初始化失败，尝试自动安装驱动...")
                _ensure_lh_driver(log_cb=log_cb)
                _log("🔁 重新初始化 LibreHardwareMonitorLib...")
                return _init_librehwmonitor(log_cb=log_cb, _retried=True)
            else:
                _log("❌ 驱动安装重试后仍然读不到温度，放弃")
                return False
        _LH_COMPUTER = computer
        _LH_CPU_HW = cpu_hw
        _LH_SENSORS = found
        _HAS_LH = True
        return True
    except Exception as e:
        import traceback
        _log(f"❌ LibreHardwareMonitor 初始化未预期异常: {e}")
        _log(traceback.format_exc())
        return False


def _close_librehwmonitor():
    global _LH_COMPUTER, _HAS_LH, _LH_SENSORS
    if _LH_COMPUTER is not None:
        try:
            _LH_COMPUTER.Close()
        except Exception:
            pass
        _LH_COMPUTER = None
    _LH_SENSORS = {}
    _HAS_LH = False


def _safe_read(sensor):
    try:
        v = sensor.Value
        if v is not None and 0 < v < 150:
            return round(float(v), 1)
    except Exception:
        pass
    return -1.0


def _get_all_cpu_temps():
    if not _HAS_LH or _LH_CPU_HW is None:
        return -1.0, -1.0, -1.0
    try:
        _LH_CPU_HW.Update()
    except Exception:
        return -1.0, -1.0, -1.0
    pkg = _safe_read(_LH_SENSORS.get("pkg"))
    mx = _safe_read(_LH_SENSORS.get("max"))
    avg = _safe_read(_LH_SENSORS.get("avg"))
    return pkg, mx, avg

# ===================== 配置区 =====================
INTERVAL = 1.0
# =================================================

def normalize_name(name: str) -> str:
    """归一化名称，去掉括号、符号、大小写，用于匹配WMI和PDH的不同命名格式"""
    name = name.lower()
    name = re.sub(r'[\(\)\[\]\s\-_]', '', name)
    return name

class CollectThread(QThread):
    sig_log = pyqtSignal(str)
    sig_count = pyqtSignal(int)
    sig_finish = pyqtSignal()
    sig_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.save_dir = _get_log_dir()
        self._stop_flag = False
        self.cpu_model = ""
        self.csv_file = None
        self.csv_writer = None
        self.current_file_path = ""
        self.line_count = 0
        self.wmi_cimv2 = None
        self.pdh_query = None
        self.h_recv = None
        self.h_send = None
        self.target_pdh_instance = None

    def get_cpu_model(self) -> str:
        try:
            for cpu in self.wmi_cimv2.Win32_Processor():
                return cpu.Name.strip()
        except Exception:
            pass
        return "UnknownCPU"

    def build_header(self) -> list:
        h = [
            "时间戳",
            "CPU封装温度", "核心最高温度", "核心平均温度",
            "CPU总使用率",
            "1分钟负载", "5分钟负载",
            "内存使用率", "可用内存(MB)",
            "网络接收速率(KB/s)", "网络发送速率(KB/s)",
            "接收速率(Mbps)", "发送速率(Mbps)",
            "磁盘读取速率(MB/s)", "磁盘写入速率(MB/s)", "CPU IO等待占比", "日志磁盘剩余空间百分比"
        ]
        return h

    def get_cpu_temps(self):
        return _get_all_cpu_temps()

    def get_active_ethernet_from_wmi(self):
        """从WMI获取已启用的有线以太网网卡列表（排除无线、虚拟、回环）"""
        self.sig_log.emit("🔍 扫描系统网卡，筛选【已启用的有线以太网】")
        adapters = self.wmi_cimv2.Win32_NetworkAdapter()
        eth_list = []
        for adp in adapters:
            name = getattr(adp, "Name", "")
            net_enabled = getattr(adp, "NetEnabled", False)
            status = getattr(adp, "NetConnectionStatus", 0)
            # 条件：网卡已启用 或 已连接（状态2=已连接）
            if not net_enabled and status != 2:
                continue
            name_lower = name.lower()
            # 排除无线、蓝牙、回环、虚拟网卡
            skip_keywords = ["wifi", "wireless", "802.11", "bluetooth", "蓝牙",
                             "loopback", "回环", "virtual", "虚拟", "wan miniport"]
            skip = False
            for kw in skip_keywords:
                if kw in name_lower:
                    skip = True
                    break
            if skip:
                continue
            eth_list.append(name)
        if eth_list:
            self.sig_log.emit(f"✅ WMI筛选到已启用以太网：{eth_list}")
        else:
            self.sig_log.emit("⚠️ WMI未筛选到符合条件的以太网，将从PDH实例中兜底选择")
        return eth_list

    def enum_pdh_nic_instances(self):
        """枚举PDH中所有网卡实例"""
        _, instances = win32pdh.EnumObjectItems(None, None, "Network Interface", win32pdh.PERF_DETAIL_WIZARD)
        valid = []
        self.sig_log.emit("==== PDH 全部网卡实例 ====")
        for inst in instances:
            self.sig_log.emit(f"  [{inst}]")
            # 过滤回环
            if "loopback" not in inst.lower():
                valid.append(inst)
        self.sig_log.emit("==========================")
        return valid

    def match_pdh_instance(self, wmi_eth_list, pdh_instances):
        """归一化匹配WMI网卡和PDH实例，找不到则兜底选第一个非无线PDH实例"""
        # 优先匹配WMI找到的以太网
        for wmi_name in wmi_eth_list:
            wmi_norm = normalize_name(wmi_name)
            for pdh_name in pdh_instances:
                pdh_norm = normalize_name(pdh_name)
                if wmi_norm == pdh_norm or wmi_norm in pdh_norm:
                    self.sig_log.emit(f"🎯 匹配成功：WMI网卡【{wmi_name}】 → PDH实例【{pdh_name}】")
                    return pdh_name

        # 兜底：从PDH实例里排除无线，选第一个
        self.sig_log.emit("⚠️ 未精确匹配，启动兜底策略，从PDH中选择第一个有线网卡")
        for pdh_name in pdh_instances:
            name_lower = pdh_name.lower()
            skip_keywords = ["wifi", "wireless", "802.11", "bluetooth", "loopback"]
            skip = False
            for kw in skip_keywords:
                if kw in name_lower:
                    skip = True
                    break
            if not skip:
                self.sig_log.emit(f"🎯 兜底选中PDH实例：【{pdh_name}】")
                return pdh_name
        return None

    def init_pdh(self, pdh_instance):
        # 直接使用字符串计数器路径，避开MakeCounterPath参数问题
        path_recv = f"\\Network Interface({pdh_instance})\\Bytes Received/sec"
        path_send = f"\\Network Interface({pdh_instance})\\Bytes Sent/sec"
        self.sig_log.emit(f"📌 接收计数器路径：{path_recv}")
        self.sig_log.emit(f"📌 发送计数器路径：{path_send}")

        self.pdh_query = win32pdh.OpenQuery()
        self.h_recv = win32pdh.AddCounter(self.pdh_query, path_recv)
        self.h_send = win32pdh.AddCounter(self.pdh_query, path_send)
        # 首次采集初始化基准样本
        win32pdh.CollectQueryData(self.pdh_query)
        # 等待一个间隔，保证第一次取值有数据
        time.sleep(INTERVAL)
        win32pdh.CollectQueryData(self.pdh_query)
        return True

    def get_net_speed_pdh(self):
        try:
            win32pdh.CollectQueryData(self.pdh_query)
            recv_val = win32pdh.GetFormattedCounterValue(self.h_recv, win32pdh.PDH_FMT_LONG)[1]
            send_val = win32pdh.GetFormattedCounterValue(self.h_send, win32pdh.PDH_FMT_LONG)[1]
            return float(recv_val), float(send_val)
        except Exception:
            return 0.0, 0.0

    def open_new_file(self, now: datetime):
        date_str = now.strftime("%Y%m%d")
        fn = f"perf_log_{date_str}_{self.cpu_model.replace(' ','_').replace(',','')}.csv"
        self.current_file_path = os.path.join(self.save_dir, fn)
        if self.csv_file is not None:
            self.csv_file.close()
        self.csv_file = open(self.current_file_path, "a", encoding="utf-8-sig", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        if os.path.getsize(self.current_file_path) == 0:
            self.csv_writer.writerow(self.build_header())
            self.line_count = 0
        self.sig_log.emit(f"✅ 日志文件已打开: {self.current_file_path}")

    def run(self):
        try:
            self.wmi_cimv2 = wmi.WMI()
            os.makedirs(self.save_dir, exist_ok=True)
            self.sig_log.emit(f"📂 保存目录：{self.save_dir}")

            lh_ok = _init_librehwmonitor(log_cb=self.sig_log.emit)
            if lh_ok:
                self.sig_log.emit("✅ LibreHardwareMonitor 初始化完成")
            else:
                self.sig_log.emit("⚠️ LibreHardwareMonitor 初始化失败（详见上方日志），温度字段将为 -1")

            # 1. WMI筛选已启用以太网
            wmi_eth_list = self.get_active_ethernet_from_wmi()
            # 2. 枚举PDH所有网卡实例
            pdh_instances = self.enum_pdh_nic_instances()
            # 3. 匹配得到最终PDH实例名
            self.target_pdh_instance = self.match_pdh_instance(wmi_eth_list, pdh_instances)

            if not self.target_pdh_instance:
                self.sig_log.emit("❌ 未找到任何有效网卡，采集终止")
                return

            # 4. 初始化PDH计数器
            if not self.init_pdh(self.target_pdh_instance):
                self.sig_log.emit("❌ PDH计数器初始化失败")
                return

            self.cpu_model = self.get_cpu_model()
            self.sig_log.emit(f"✅ CPU型号: {self.cpu_model}")

            now = datetime.now()
            self.open_new_file(now)
            today = now.date()

            prev_disk = psutil.disk_io_counters()
            self.sig_log.emit("🚀 采集开始（仅以太网，速率与任务管理器同源）")

            while not self._stop_flag:
                loop_start = time.perf_counter()
                now = datetime.now()
                if now.date() != today:
                    today = now.date()
                    self.open_new_file(now)

                ts = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                pkg_temp, core_max_temp, core_avg_temp = self.get_cpu_temps()
                cpu_total_pct = psutil.cpu_percent(interval=None)

                load1, load5 = 0.0, 0.0
                mem = psutil.virtual_memory()
                mem_pct = mem.percent
                mem_avail_mb = round(mem.available / 1024 / 1024, 2)

                recv_byte_sec, send_byte_sec = self.get_net_speed_pdh()
                recv_kb = recv_byte_sec / 1024.0
                send_kb = send_byte_sec / 1024.0
                # 和任务管理器完全一致的Mbps换算：十进制、比特
                recv_mbps = recv_byte_sec * 8 / 1000000.0
                send_mbps = send_byte_sec * 8 / 1000000.0

                disk = psutil.disk_io_counters()
                disk_read_mb = (disk.read_bytes - prev_disk.read_bytes) / 1024 / 1024 / INTERVAL
                disk_write_mb = (disk.write_bytes - prev_disk.write_bytes) / 1024 / 1024 / INTERVAL
                prev_disk = disk

                io_wait = 0.0
                try:
                    io_wait = psutil.cpu_times_percent(interval=None).iowait
                except Exception:
                    pass
                usage = psutil.disk_usage(self.save_dir)
                free_pct = round(usage.free / usage.total * 100, 2)

                row = [
                    ts,
                    pkg_temp, core_max_temp, core_avg_temp,
                    cpu_total_pct,
                    round(load1,2), round(load5,2),
                    mem_pct, mem_avail_mb,
                    round(recv_kb,2), round(send_kb,2),
                    round(recv_mbps,2), round(send_mbps,2),
                    round(disk_read_mb,2), round(disk_write_mb,2), round(io_wait,2), free_pct
                ]
                self.csv_writer.writerow(row)
                self.csv_file.flush()
                self.line_count += 1
                self.sig_count.emit(self.line_count)

                cost = time.perf_counter() - loop_start
                sleep_time = max(0.0, INTERVAL - cost)
                time.sleep(sleep_time)

            if self.csv_file is not None:
                self.csv_file.close()
            if self.pdh_query:
                win32pdh.CloseQuery(self.pdh_query)
            _close_librehwmonitor()
            self.sig_log.emit("🛑 采集已停止")
        except Exception as e:
            self.sig_error.emit(f"❌ 采集线程致命异常: {str(e)}")
            if self.csv_file is not None:
                self.csv_file.close()
            if self.pdh_query:
                win32pdh.CloseQuery(self.pdh_query)
            _close_librehwmonitor()
        finally:
            self.sig_finish.emit()

    def stop(self):
        self._stop_flag = True


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("性能采集工具V1.0")
        self.resize(780, 560)
        self.collect_thread = None

        self.btn_start = QPushButton("▶ 启动采集")
        self.btn_stop = QPushButton("■ 停止采集")
        self.btn_stop.setEnabled(False)
        self.label_count = QLabel("已写入行数：0")
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addWidget(self.label_count)
        layout.addWidget(self.btn_start)
        layout.addWidget(self.btn_stop)
        layout.addWidget(self.txt_log)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)

    def append_log(self, text):
        self.txt_log.append(text)
        cursor = self.txt_log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.txt_log.setTextCursor(cursor)

    def update_count(self, cnt):
        self.label_count.setText(f"已写入行数：{cnt}")

    def on_start(self):
        if self.collect_thread is not None and self.collect_thread.isRunning():
            return
        self.txt_log.clear()
        self.label_count.setText("已写入行数：0")

        self.collect_thread = CollectThread()
        self.collect_thread.sig_log.connect(self.append_log)
        self.collect_thread.sig_count.connect(self.update_count)
        self.collect_thread.sig_finish.connect(self.on_thread_end)
        self.collect_thread.sig_error.connect(self.append_log)
        self.collect_thread.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def on_stop(self):
        if self.collect_thread and self.collect_thread.isRunning():
            self.collect_thread.stop()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)

    def on_thread_end(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def closeEvent(self, event):
        if self.collect_thread and self.collect_thread.isRunning():
            self.collect_thread.stop()
            self.collect_thread.wait()
        event.accept()


if __name__ == "__main__":
    _elevate()
    app = QApplication([])
    w = MainWindow()
    w.show()
    app.exec()