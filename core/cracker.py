# -*- coding: utf-8 -*-
"""
文件名称：cracker.py
功能描述：Hashcat 调用与破解调度模块
         负责封装 Hashcat 命令行参数、启动子进程执行GPU破解、
         解析实时进度与状态输出、读取最终找回密码结果。
         【骨架阶段】：完成接口框架 + 核心命令参数拼接 + 状态回调占位，
         后续迭代补全 Hashcat --machine-readable / --status 格式的精确正则解析。
创建日期：2026-08-03
修改记录：
    2026-08-03  初始版本：完成 CrackConfig / CrackResult / CrackProgress 数据结构
                         与 HashcatExecutor 骨架接口
"""

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, List, Dict

# 兼容两种运行方式：作为 core 包 import 或直接 python core/xxx.py 调试
try:
    from .path_manager import PathManager, ToolPaths
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from core.path_manager import PathManager, ToolPaths  # type: ignore[no-redef]


class AttackMode(Enum):
    """
    Hashcat 攻击模式（-a 参数）
    参考：hashcat --help | grep -A1 "Attack modes"
    """
    DICT = 0          # 0 = Straight（字典攻击，最常用）
    COMBINATION = 1   # 1 = Combination（两个字典拼接）
    MASK = 3          # 3 = Brute-force / Mask（掩码暴力破解）
    HYBRID_DICT_MASK = 6  # 6 = Hybrid Wordlist + Mask（字典+掩码后缀）
    HYBRID_MASK_DICT = 7  # 7 = Hybrid Mask + Wordlist（掩码前缀+字典）


class CrackStatus(Enum):
    """破解任务状态枚举"""
    IDLE = "idle"            # 未开始
    RUNNING = "running"      # 运行中
    CRACKED = "cracked"      # 已找到密码
    EXHAUSTED = "exhausted"  # 组合/字典全部试完未找到
    ERROR = "error"          # 执行异常
    STOPPED = "stopped"      # 用户手动终止


@dataclass
class CrackConfig:
    """
    一次破解任务所需的全部参数
    """
    hash_file_path: str                    # 哈希文件路径（ExtractResult.hash_file_path 或手工传入）
    hashcat_mode: int                      # Hashcat -m 参数（13600 ZIP / 12500 RAR3 / 13000 RAR5 / 11600 7Z）
    attack_mode: AttackMode = AttackMode.DICT   # 攻击模式，默认字典
    dictionary_paths: Optional[List[str]] = None  # 字典文件列表（-a 0 时必填）
    mask: Optional[str] = None              # 掩码字符串，如 "?d?d?d?d?d?d"（-a 3 / 6 / 7 时必填）
    rules_file: Optional[str] = None        # 规则文件路径，如 hashcat/rules/best64.rule
    extra_args: List[str] = field(default_factory=list)  # 用户自定义额外参数
    work_load_profile: int = 3              # 工作负载 1~4，默认 3（推荐）
    enable_potfile: bool = True             # 是否启用 potfile（保留历史破解结果，避免重复跑）
    force: bool = True                      # 忽略警告强制运行（便于无头脚本）
    # 设备类型: "auto"(默认自动选择) / "force_gpu"(-D 2) / "force_cpu"(-D 1)
    # 注:废弃旧字段 gpu_enabled,改为 device_type 细分控制
    device_type: str = "auto"


@dataclass
class CrackProgress:
    """
    破解进度快照（供 UI 面板刷新）
    具体字段来源于 Hashcat --status 输出
    """
    status: CrackStatus = CrackStatus.IDLE
    speed_hs: float = 0.0                   # 当前速度（H/s 每秒哈希数）
    progress_percent: float = 0.0           # 进度百分比 0~100
    tried_count: int = 0                    # 已尝试数量
    total_count: int = 0                    # 总候选数量
    eta_seconds: Optional[int] = None       # 预估剩余秒数
    recovered: int = 0                      # 已恢复密码数
    total_hashes: int = 1                   # 总哈希数
    raw_line: str = ""                      # 原始进度行（调试用）
    # 新增字段:用于 UI 显示"破解顺序"和绝对进度
    progress_abs: str = ""                  # 绝对进度数 "18/36"
    candidates: str = ""                    # 当前候选密码区间 "a -> 9"


@dataclass
class CrackResult:
    """
    破解任务最终结果
    """
    success: bool                           # 是否成功（找到密码）
    status: CrackStatus                     # 最终状态
    recovered_passwords: Dict[str, str]     # 已找回密码 { hash_string : plain_text }
    potfile_path: Optional[str]             # 本次使用的 potfile 路径
    error_message: Optional[str] = None     # 错误信息
    duration_seconds: float = 0.0           # 总耗时（秒）


ProgressCallback = Callable[[CrackProgress], None]


class HashcatExecutor:
    """
    Hashcat 执行器
    负责：
        1. 校验输入参数完整性
        2. 拼接完整命令行参数
        3. 切换工作目录到 hashcat_root（非常重要！否则找不到 kernels/、OpenCL/）
        4. 启动子进程并逐行解析 stdout
        5. 通过 ProgressCallback 向 UI 推送进度
        6. 任务结束后读取 potfile 或 cracker stdout 提取明文密码

    使用方法（骨架示例）：
        from core import PathManager, HashcatExecutor, CrackConfig, AttackMode
        cfg = CrackConfig(
            hash_file_path="data/output/demo_zip.hash",
            hashcat_mode=13600,
            attack_mode=AttackMode.DICT,
            dictionary_paths=["data/dictionaries/common.txt"],
        )
        exe = HashcatExecutor(PathManager())
        prog_cb = lambda p: print(f"{p.progress_percent:.1f}%  {p.speed_hs:.0f} H/s")
        result = exe.run(cfg, progress_callback=prog_cb)
    """

    # Hashcat 结果行正则（骨架阶段简单匹配，后续可替换为 --machine-readable 解析）
    # 形如： "hash:password" 或  STATUS  CRACKED
    _RE_CRACKED_LINE = re.compile(r"^(.+?):(.+)$")

    def __init__(self, path_manager: PathManager):
        self.pm = path_manager
        self.tool_paths: ToolPaths = self.pm.discover()
        self.data_dirs: dict = self.pm.ensure_data_dirs()
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # ==================================================================
    # 基础校验
    # ==================================================================
    def is_available(self) -> bool:
        """快速检查：hashcat 主程序和根目录是否可用"""
        return bool(self.tool_paths.hashcat and self.tool_paths.hashcat_root)

    def list_devices(self) -> str:
        """
        枚举 GPU/CPU 设备（对应 hashcat -I）
        返回原始设备信息文本，供 UI 「GPU信息」面板显示。
        """
        if not self.is_available():
            return "[ERROR] 未找到 Hashcat，请检查 bin/ 目录。"
        try:
            workdir = self.tool_paths.hashcat_root
            completed = subprocess.run(
                [self.tool_paths.hashcat, "--force", "-I"],
                capture_output=True, text=True, timeout=30,
                cwd=workdir,
            )
            out = (completed.stdout or "") + (completed.stderr or "")
            return out.strip() or "(hashcat -I 无输出)"
        except subprocess.TimeoutExpired:
            return "[TIMEOUT] Hashcat 设备枚举超时(>30s)"
        except Exception as exc:
            return f"[EXCEPTION] {type(exc).__name__}: {exc}"

    # ==================================================================
    # 命令拼接
    # ==================================================================
    def build_command(self, cfg: CrackConfig) -> List[str]:
        """
        根据 CrackConfig 构建完整的 Hashcat 命令行参数列表
        """
        if not self.tool_paths.hashcat:
            raise RuntimeError("hashcat.exe 路径未解析，无法构建命令")

        cmd: List[str] = [self.tool_paths.hashcat]
        # 基础参数
        cmd += ["--hash-type", str(cfg.hashcat_mode)]
        cmd += ["--attack-mode", str(cfg.attack_mode.value)]
        cmd += ["--workload-profile", str(cfg.work_load_profile)]
        # 设备类型选择:
        # - "auto"(默认): hashcat 自动选设备,RTX 5060 等独立显卡会被识别并使用
        # - "force_gpu": 加 -D 2 强制 OpenCL 设备类型为 GPU(驱动不兼容时可能产出假密码)
        # - "force_cpu": 加 -D 1 强制走 CPU(用于无 GPU 或驱动异常环境)
        # 注:之前 gpu_enabled=True 直接加 -D 2 + --force 导致 hashcat 产出
        #     "https://hashcat.net/faq/wrongdriver" 假密码,已废弃
        device_type = getattr(cfg, "device_type", "auto")
        if device_type == "force_gpu":
            cmd += ["-D", "2"]
        elif device_type == "force_cpu":
            cmd += ["-D", "1"]
        # "auto" 不加 -D 参数,让 hashcat 自行选择
        if cfg.force:
            cmd.append("--force")
        # 启用状态输出:每 2 秒输出一次进度,供 progress_callback 解析
        # (不加 --status 时 hashcat 仅在结束时输出,实时进度区无数据)
        cmd += ["--status", "--status-timer=2"]
        if not cfg.enable_potfile:
            cmd += ["--potfile-disable"]
        else:
            # 指定 potfile 输出到 data/output，避免写 Hashcat 安装目录（权限问题）
            pot_path = Path(self.data_dirs["output"]) / "hashcat.potfile"
            cmd += ["--potfile-path", str(pot_path.resolve())]
        if cfg.rules_file:
            cmd += ["--rules-file", cfg.rules_file]
        cmd.extend(cfg.extra_args)
        # hash 文件
        cmd.append(cfg.hash_file_path)
        # 字典 / 掩码
        if cfg.attack_mode == AttackMode.DICT:
            if not cfg.dictionary_paths:
                raise ValueError("AttackMode=DICT 时 dictionary_paths 不可为空")
            cmd.extend(cfg.dictionary_paths)
        elif cfg.attack_mode == AttackMode.COMBINATION:
            if cfg.dictionary_paths is None or len(cfg.dictionary_paths) < 2:
                raise ValueError("AttackMode=COMBINATION 至少需要两个字典")
            cmd.extend(cfg.dictionary_paths[:2])
        elif cfg.attack_mode == AttackMode.MASK:
            if not cfg.mask:
                raise ValueError("AttackMode=MASK 时 mask 不可为空")
            cmd.append(cfg.mask)
        elif cfg.attack_mode in (AttackMode.HYBRID_DICT_MASK, AttackMode.HYBRID_MASK_DICT):
            if not cfg.mask or not cfg.dictionary_paths:
                raise ValueError("Hybrid 模式需同时提供字典和掩码")
            cmd.append(cfg.dictionary_paths[0])
            cmd.append(cfg.mask)
        return cmd

    # ==================================================================
    # 执行主流程（骨架）
    # ==================================================================
    def run(self, cfg: CrackConfig,
            progress_callback: Optional[ProgressCallback] = None) -> CrackResult:
        """
        同步执行一次破解任务（阻塞至任务结束/异常）
        后续可扩展 run_async() 用线程包装，配合 Textual worker 机制不阻塞 UI。
        【骨架阶段】命令执行、结果文件处理就位；进度解析先用「原始行透传+基础字段占位」，
        下一个迭代按 Hashcat --machine-readable 标准精确补全正则。
        """
        start_ts = time.time()
        self._stop_event.clear()
        potfile_path = str(
            Path(self.data_dirs["output"]).resolve() / "hashcat.potfile"
        ) if cfg.enable_potfile else None

        # 1. 前置检查
        if not self.is_available():
            return self._quick_result(CrackStatus.ERROR, {}, potfile_path,
                                      "Hashcat 不可用：未找到主程序或根目录")
        if not Path(cfg.hash_file_path).exists():
            return self._quick_result(CrackStatus.ERROR, {}, potfile_path,
                                      f"hash 文件不存在: {cfg.hash_file_path}")

        # 2. 构建命令并启动
        try:
            cmd = self.build_command(cfg)
        except (ValueError, RuntimeError) as exc:
            return self._quick_result(CrackStatus.ERROR, {}, potfile_path, str(exc))

        workdir = self.tool_paths.hashcat_root
        progress = CrackProgress(status=CrackStatus.RUNNING)
        if progress_callback:
            try:
                progress_callback(progress)
            except Exception:
                pass  # 回调异常不影响主流程

        recovered: Dict[str, str] = {}
        last_error: Optional[str] = None
        # 已知假密码标识:hashcat 驱动异常时会把这些字符串当作"密码"输出到 potfile
        # (实测 --force + -D 2 在 CUDA SDK 未装环境会产出 wrongdriver URL)
        FAKE_PASSWORD_MARKERS = (
            "hashcat.net/faq/wrongdriver",
            "hashcat.net/faq",
            "No device found",
            "Invalid argument",
        )

        def _is_fake_password(pwd: str) -> bool:
            """检测是否为 hashcat 错误状态产出的假密码"""
            if not pwd:
                return True
            for marker in FAKE_PASSWORD_MARKERS:
                if marker in pwd:
                    return True
            return False

        try:
            # 启动子进程（text=True 流式读 stdout）
            # 注:stdin=DEVNULL 避免 hashcat 在 --status 模式下等待交互输入
            #     ([s]tatus [p]ause [b]ypass... 提示会阻塞进程退出,导致卡死)
            self._proc = subprocess.Popen(
                cmd, cwd=workdir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, bufsize=1, errors="replace",
            )
            assert self._proc.stdout is not None
            for raw_line in self._proc.stdout:
                if self._stop_event.is_set():
                    self._proc.terminate()
                    break
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                # --- 原始行写入 progress.raw_line ---
                progress.raw_line = line

                # 1. 识别形如 "hash:password" 的恢复行(破解成功)
                # 注:hashcat 启动时会输出大量含 ":" 的信息行,必须严格过滤
                #   误识别示例:"Minimum password length supported by kernel: 0"
                #   真实示例:"$rar5$16$...:111"
                m = self._RE_CRACKED_LINE.match(line)
                if m and (":" in line) and not line.startswith("hashcat") and not line.startswith("#"):
                    lhs, rhs = m.group(1), m.group(2)
                    # 严格判定:左半必须以 $ 开头(真实 hash 格式)
                    # (之前用 "$ in lhs or len(lhs)>32" 太宽松,把启动信息误识别为 hash)
                    if lhs.startswith("$") and rhs and not _is_fake_password(rhs):
                        recovered[lhs] = rhs
                        # 关键修复:破解成功后立即终止 hashcat 进程并退出循环
                        # 原因:hashcat --status 模式下,即使破解成功进程也不会自动退出,
                        #       stdout 循环会一直阻塞,导致 worker 线程卡死,
                        #       _crack_dict_running 永远为 True,UI 无法继续操作
                        try:
                            self._proc.terminate()
                        except Exception:  # noqa: BLE001
                            pass
                        break

                # 2. 解析 --status 输出的分段状态行
                # hashcat --status 输出格式(每 2 秒一次):
                #   Status...........: Running
                #   Speed.#01........:      362 H/s (2.78ms) @ Accel:16 Loops:1024 Thr:256 Vec:1
                #   Progress.........: 18/36 (50.00%)
                #   Recovered........: 0/1 (0.00%) Digests (total)
                #   Guess.Queue......: 1/1 (100.00%)
                if line.startswith("Status"):
                    # 提取状态值: Status...........: Running
                    val = line.split(":", 1)[1].strip() if ":" in line else ""
                    status_map = {
                        "Running": CrackStatus.RUNNING,
                        "Cracked": CrackStatus.CRACKED,
                        "Exhausted": CrackStatus.EXHAUSTED,
                        "Stopped": CrackStatus.STOPPED,
                        "Error": CrackStatus.ERROR,
                        "Quit": CrackStatus.STOPPED,
                        "Bypass": CrackStatus.RUNNING,
                        "Aborted": CrackStatus.STOPPED,
                    }
                    if val in status_map:
                        progress.status = status_map[val]
                        # 终态处理(关键修复):
                        # - Cracked/Error/Stopped/Quit/Aborted:hashcat --status 模式下
                        #   进程不会自动退出,必须立即 terminate 并退出循环
                        # - Exhausted:不能立即终止!暴力穷举/掩码使用 --increment 增量时,
                        #   hashcat 每完成一个长度段就输出一次 "Status: Exhausted"
                        #   (如 Guess.Queue 1/2),这是"阶段结束"而非"任务结束";
                        #   只有全部段试完后 hashcat 才会自动退出(return 1),
                        #   stdout 自然 EOF,循环随之结束。
                        #   此前把每次 Exhausted 都当终态,导致增量模式永远只试第一段
                        #   就提前终止,暴力穷举形同不可用(实测仅 36 个候选即结束)。
                        if val in ("Cracked", "Error", "Stopped",
                                   "Quit", "Aborted"):
                            try:
                                self._proc.terminate()
                            except Exception:  # noqa: BLE001
                                pass
                            break
                elif line.startswith("Speed"):
                    # 提取速度: Speed.#01........:      362 H/s
                    speed = self._extract_speed(line)
                    if speed is not None:
                        progress.speed_hs = speed
                elif line.startswith("Progress"):
                    # 提取进度: Progress.........: 18/36 (50.00%)
                    pct = self._extract_percent(line)
                    if pct is not None:
                        progress.progress_percent = pct
                    # 提取绝对进度数: 18/36
                    m_abs = re.search(r"(\d+)\s*/\s*(\d+)", line)
                    if m_abs:
                        progress.progress_abs = f"{m_abs.group(1)}/{m_abs.group(2)}"
                elif line.startswith("Candidates"):
                    # 提取候选密码区间: Candidates.#01...: a -> 9
                    # (当前正在尝试的密码起止,用于显示"破解顺序")
                    m_cand = re.search(r"Candidates\.\S+\s*:\s*(.+)", line)
                    if m_cand:
                        progress.candidates = m_cand.group(1).strip()
                elif line.startswith("Recovered"):
                    # 提取已恢复数: Recovered........: 1/1 (100.00%) Digests
                    if progress.recovered == 0:
                        m_rec = re.search(r"(\d+)\s*/\s*\d+", line)
                        if m_rec and int(m_rec.group(1)) > 0:
                            progress.recovered = int(m_rec.group(1))
                # 兼容旧行格式:同行同时含 % 和 H/s
                elif "%" in line and ("H/s" in line or "MH/s" in line or "KH/s" in line or "GH/s" in line):
                    pct = self._extract_percent(line)
                    if pct is not None:
                        progress.progress_percent = pct
                    speed = self._extract_speed(line)
                    if speed is not None:
                        progress.speed_hs = speed

                # 每收到一行就回调(让 UI 实时刷新)
                if progress_callback:
                    try:
                        progress_callback(progress)
                    except Exception:
                        pass
            # stdout 已关闭或循环 break,用 poll 非阻塞获取退出码
            # (之前 wait(timeout=10) 在 hashcat --status 模式下会卡死:
            #  进程已退出但 Windows pipe 缓冲未清理,wait 会阻塞到超时)
            return_code = self._proc.poll()
            if return_code is None:
                # 进程未退出:terminate 后等1秒,仍不退出则 kill 兜底
                # (terminate 发送 CTRL_BREAK,hashcat 可能不响应;kill 强制结束)
                try:
                    self._proc.terminate()
                    try:
                        return_code = self._proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                        return_code = -1
                except Exception:  # noqa: BLE001
                    try:
                        self._proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    return_code = -1
        except Exception as exc:  # noqa: BLE001
            last_error = f"进程异常: {type(exc).__name__}: {exc}"
            return_code = -1
        finally:
            self._proc = None

        # 3. potfile 兜底补全(必须在状态判定之前执行)
        # 原因:break 退出循环时,密码可能只在 potfile 里,stdout 的 recovered 为空
        #       若先判状态再补 potfile,会把破解成功误判为 ERROR(return_code=-1)
        #       (_merge_potfile 内部已过滤假密码,可安全填充 recovered)
        if cfg.enable_potfile and Path(potfile_path).exists():
            self._merge_potfile(potfile_path, cfg.hash_file_path, recovered)

        # 4. 根据退出码 + recovered + progress.status 判定最终状态
        # 判定优先级:手动停止 > recovered 非空 > stdout 状态行 > 退出码
        status = CrackStatus.ERROR
        if self._stop_event.is_set():
            status = CrackStatus.STOPPED
        elif recovered:
            # recovered 非空:无论 return_code 是什么(含 kill 后的 -1)都判破解成功
            status = CrackStatus.CRACKED
        elif progress.status == CrackStatus.CRACKED:
            # stdout 已报告 Status: Cracked(即使 potfile 没补全也判成功)
            status = CrackStatus.CRACKED
        elif return_code == 0:
            # Hashcat 退出码:0=已破解(需有 recovered 确认) 1=未破解 2=错误
            status = CrackStatus.CRACKED if recovered else CrackStatus.EXHAUSTED
        elif return_code == 1:
            status = CrackStatus.EXHAUSTED
        else:
            status = CrackStatus.ERROR
            if last_error is None:
                last_error = f"Hashcat 返回码 {return_code}"

        # 回调最终状态
        progress.status = status
        progress.progress_percent = 100.0 if status == CrackStatus.CRACKED else progress.progress_percent
        if progress_callback:
            try:
                progress_callback(progress)
            except Exception:
                pass

        return CrackResult(
            success=(status == CrackStatus.CRACKED and len(recovered) > 0),
            status=status,
            recovered_passwords=recovered,
            potfile_path=potfile_path,
            error_message=last_error,
            duration_seconds=time.time() - start_ts,
        )

    def stop(self) -> None:
        """外部请求终止当前正在运行的破解任务（线程安全）"""
        self._stop_event.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    # ==================================================================
    # 私有工具方法
    # ==================================================================
    @staticmethod
    def _quick_result(status: CrackStatus, recovered: Dict[str, str],
                      potfile_path: Optional[str], err: str) -> CrackResult:
        return CrackResult(
            success=False, status=status, recovered_passwords=recovered,
            potfile_path=potfile_path, error_message=err, duration_seconds=0.0,
        )

    @staticmethod
    def _extract_percent(line: str) -> Optional[float]:
        """骨架阶段：简单抓第一个类似 12.34% 的子串"""
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_speed(line: str) -> Optional[float]:
        """骨架阶段：简单抓形如 '12345.6 H/s'、'12.3 MH/s' 的速率"""
        patterns = [
            (r"([\d\.]+)\s*GH/s", 1e9),
            (r"([\d\.]+)\s*MH/s", 1e6),
            (r"([\d\.]+)\s*KH/s", 1e3),
            (r"([\d\.]+)\s*H/s",  1.0),
        ]
        for pat, mult in patterns:
            m = re.search(pat, line)
            if m:
                try:
                    return float(m.group(1)) * mult
                except ValueError:
                    pass
        return None

    @staticmethod
    def _merge_potfile(potfile_path: str, hash_file_path: str,
                       recovered: Dict[str, str]) -> None:
        """
        从 potfile 中读取所有 hash:password，过滤出本次 hash 文件中的条目，
        补齐 stdout 可能漏读的恢复结果。
        注:会过滤假密码(如 hashcat 驱动异常产出的 wrongdriver URL)
        """
        # 已知假密码标识(与 run() 内一致)
        FAKE_MARKERS = (
            "hashcat.net/faq/wrongdriver",
            "hashcat.net/faq",
            "No device found",
            "Invalid argument",
        )

        def _is_fake(pwd: str) -> bool:
            if not pwd:
                return True
            return any(m in pwd for m in FAKE_MARKERS)

        try:
            hashes_in_task: set = set()
            if Path(hash_file_path).exists():
                for ln in Path(hash_file_path).read_text(encoding="utf-8", errors="replace").splitlines():
                    ln = ln.strip()
                    if ln:
                        hashes_in_task.add(ln)
            if Path(potfile_path).exists():
                for ln in Path(potfile_path).read_text(encoding="utf-8", errors="replace").splitlines():
                    if ":" in ln:
                        h, p = ln.split(":", 1)
                        if h in hashes_in_task and not _is_fake(p):
                            recovered[h] = p
                        elif h in hashes_in_task and _is_fake(p):
                            # 假密码:不写入 recovered,避免误报破解成功
                            pass
        except Exception:
            # potfile 读失败是次要问题，不抛错
            pass


if __name__ == "__main__":
    # 调试入口：先枚举 GPU 设备（无需任务参数）
    import sys
    exe = HashcatExecutor(PathManager())
    print("=" * 60)
    print("Hashcat 可用性:", "OK" if exe.is_available() else "NO")
    print("设备信息 (-I):")
    print(exe.list_devices())
    print()
    print("命令示例打印:")
    sample_cfg = CrackConfig(
        hash_file_path="sample.hash", hashcat_mode=13600,
        attack_mode=AttackMode.DICT,
        dictionary_paths=["common.dict"],
    )
    if exe.is_available():
        try:
            print(" ".join(exe.build_command(sample_cfg)))
        except Exception as exc:
            print("构建命令失败:", exc)
