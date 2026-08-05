# -*- coding: utf-8 -*-
"""
文件名称：dict_generator.py
功能描述：字典生成模块骨架
         负责根据用户配置生成候选密码字典文件，提供以下四种基础模式：
             1. 内置常见弱口令字典 + 自定义字典合并
             2. 字符集组合（如 6位纯数字、8位大小写+数字）
             3. Hashcat 标准掩码模式（?d?l?u?s，兼容 Hashcat 原生语法）
             4. 基于基础字典 + 规则变异（大小写互换、数字后缀等）
         【骨架阶段】：完成接口框架、文件写入、配置结构定义，
         后续迭代补全 itertools 组合生成与规则变异引擎。
创建日期：2026-08-03
修改记录：
    2026-08-03  初始版本：完成 GenConfig / GenResult 数据结构与生成器骨架
"""

import os
import itertools
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Callable, Iterable

# 兼容两种运行方式：
#   - 作为 core 包的一部分被外部 import（用相对导入）
#   - 直接 `python core/xxx.py` 调试运行（把项目根加入 sys.path 后用绝对导入）
try:
    from .path_manager import PathManager
except ImportError:  # pragma: no cover - 直接运行模块时走该分支
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from core.path_manager import PathManager  # type: ignore[no-redef]


class GenMode(Enum):
    """字典生成模式枚举"""
    PRESET = "preset"              # 使用预置弱口令 + 合并已有字典文件
    CHARSET_COMB = "charset"       # 指定字符集 + 固定长度，笛卡尔积组合
    MASK = "mask"                  # Hashcat 兼容掩码语法
    RULE = "rule"                  # 基础字典 + 变异规则
    SOCIAL = "social"              # 社工字典:基于目标信息自动组合变异


# Hashcat 标准掩码符号 -> 字符集 映射表（骨架阶段用 Python 原生生成时用）
MASK_MAP: Dict[str, str] = {
    "?l": "abcdefghijklmnopqrstuvwxyz",        # ?l = 小写字母
    "?u": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",        # ?u = 大写字母
    "?d": "0123456789",                        # ?d = 数字
    "?s": "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~",  # ?s = 特殊字符
    "?a": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~",  # ?a = all
    "?b": "".join(chr(i) for i in range(0x00, 0xFF + 1)),  # ?b = 0x00~0xFF
}


# 常用预置弱口令清单（骨架阶段示例，后续可拆成 data/dictionaries/*.txt 独立文件）
DEFAULT_WEAK_PASSWORDS: List[str] = [
    "123456", "12345678", "123456789", "password", "admin",
    "123123", "111111", "000000", "888888", "666666",
    "1234567890", "qwerty", "abc123", "qwe123", "password1",
    "zxcvbnm", "asdfgh", "1q2w3e", "1qaz2wsx", "root",
    "toor", "guest", "test", "test123", "p@ssw0rd",
    "1qaz@WSX", "pass@123", "admin123", "123qwe", "123abc",
]


# CJK 基本汉字范围:U+4E00 ~ U+9FFF(覆盖绝大多数常用中文)
# 用于过滤纯中文×纯中文组合,这类组合作为压缩包密码命中率极低
_CJK_RANGE_START = "\u4e00"
_CJK_RANGE_END = "\u9fff"


def _is_pure_cjk(s: str) -> bool:
    """判断字符串是否全部由 CJK 基本汉字组成
    :param s: 待判断字符串
    :return: True=纯中文,False=含其他字符(英文/数字/符号/空)
    用途:社工字典生成时过滤"中文姓名×中文公司"等纯中文组合
          这类组合作为压缩包密码命中率极低,纯属噪音
    """
    if not s:
        return False
    return all(_CJK_RANGE_START <= c <= _CJK_RANGE_END for c in s)


@dataclass
class GenConfig:
    """字典生成配置"""
    output_file: str                              # 输出字典文件路径（必填）
    mode: GenMode = GenMode.PRESET                # 生成模式
    # PRESET 模式
    include_builtin_weak: bool = True             # 是否包含内置弱口令
    extra_dict_files: List[str] = field(default_factory=list)  # 额外字典文件列表（绝对路径）
    # CHARSET_COMB 模式
    charset: Optional[str] = None                 # 自定义字符集，如 "0123456789abcdef"
    min_length: int = 4                           # 最小长度
    max_length: int = 6                           # 最大长度（含）
    # MASK 模式
    mask: Optional[str] = None                    # Hashcat 掩码，如 "?d?d?d?d?d?d"
    # RULE 模式
    base_dict_file: Optional[str] = None          # 基础字典文件
    rule_names: List[str] = field(default_factory=list)  # 启用的规则名（见 _RULE_FUNCTIONS 表）
    # 通用选项
    remove_duplicates: bool = True                # 去重
    encoding: str = "utf-8"                       # 输出编码
    line_ending: str = "\n"                       # 换行符
    # 生成数量限制:0=不限制(全部生成);>0=只生成指定行数(去重后计数,达到即停)
    # 用途:控制超大字典输出,避免磁盘撑爆;不足时按实际数量生成,不补齐
    max_lines: int = 0


@dataclass
class GenResult:
    """字典生成结果"""
    success: bool
    output_file: Optional[str]
    total_lines: int
    size_bytes: int
    error_message: Optional[str] = None
    duration_seconds: float = 0.0


# ========================================================================
# 社工字典生成配置
# 收集目标人物的多维度信息,系统自动组合变异生成候选密码
# 字段说明:
#   - 基础信息:姓名/昵称/生日/手机/QQ/邮箱/身份证
#   - 工作/学校:公司/职位/工号/学校/入学年份
#   - 家庭/其他:配偶/子女/宠物/纪念日/车牌
#   - 习惯/其他:喜好词/幸运数字/区号/常用符号后缀
# 所有字段均为可选,填哪些用哪些,系统自动判断组合
# ========================================================================
@dataclass
class SocialConfig:
    """社工字典生成配置(所有字段可选,填哪些用哪些)"""
    # === 基础信息 ===
    name_cn: str = ""               # 中文姓名(如:张三)
    name_pinyin: str = ""           # 拼音全拼(如:zhangsan)
    name_en: str = ""               # 英文名(如:zhang)
    nickname: str = ""              # 昵称/网名(如:小张)
    birth_year: str = ""            # 生日年份(如:1990)
    birth_month: str = ""           # 生日月份(如:01 或 1)
    birth_day: str = ""             # 生日日期(如:15)
    birth_full: str = ""            # 完整生日(如:19900115 或 1990-01-15)
    phone: str = ""                 # 手机号(如:13800138000)
    qq: str = ""                    # QQ号
    wechat: str = ""                # 微信号(如:zhangsan_wx)
    email: str = ""                 # 邮箱(如:zhangsan@qq.com)
    id_card: str = ""               # 身份证号(完整或后6位)
    # === 工作/学校 ===
    company: str = ""               # 公司名(如:腾讯)
    position: str = ""              # 职位(如:工程师)
    employee_id: str = ""           # 工号
    school: str = ""                # 学校名(如:清华)
    school_year: str = ""           # 入学年份(如:2008)
    # === 家庭/其他 ===
    spouse_name: str = ""           # 配偶姓名
    child_name: str = ""            # 子女姓名
    pet_name: str = ""              # 宠物名
    anniversary: str = ""           # 纪念日(如:20151001)
    car_plate: str = ""             # 车牌号(如:京A12345)
    # === 习惯/其他 ===
    favorite_words: str = ""        # 喜好词汇(逗号分隔,如:happy,lucky)
    lucky_numbers: str = ""         # 幸运数字(逗号分隔,如:6,8,9)
    area_code: str = ""             # 地区区号(如:010 或 0755)
    common_suffixes: str = ""       # 常用符号后缀(逗号分隔,如:@,#,123,!@#)
    # === 通用选项 ===
    output_file: str = ""           # 输出字典文件路径(必填)
    encoding: str = "utf-8"         # 输出编码
    line_ending: str = "\n"         # 换行符
    remove_duplicates: bool = True  # 去重


# ========================================================================
# 规则模式支持的变异规则集合（骨架阶段示例，后续可扩展）
# 每个规则函数输入一行密码，输出变异后的字符串迭代器
# ========================================================================
_RULE_FUNCTIONS: Dict[str, Callable[[str], Iterable[str]]] = {
    # 原样保留
    "noop": lambda s: (s for _ in [s]),
    # 全部小写
    "lower": lambda s: (s.lower(),),
    # 全部大写
    "upper": lambda s: (s.upper(),),
    # 首字母大写
    "capitalize": lambda s: (s.capitalize(),),
    # 追加 0~99 两位数字后缀
    "suffix_num_00_99": lambda s: (f"{s}{d:02d}" for d in range(100)),
    # 追加 2000~2030 年份后缀（常见生日、密码过期）
    "suffix_year_2000_2030": lambda s: (f"{s}{y}" for y in range(2000, 2031)),
    # 追加经典组合：@123 / #123 / !123
    "suffix_at_123": lambda s: (f"{s}{x}" for x in ("@123", "#123", "!123")),
    # 数字 + 特殊字符 后戳混搭
    "suffix_0_9_a": lambda s: (f"{s}{d}{c}" for d in "0123456789" for c in "@#!%*"),
}


class DictGenerator:
    """
    字典生成器（骨架）
    调用示例：
        cfg = GenConfig(output_file="dict_gen.txt", mode=GenMode.PRESET)
        g = DictGenerator(PathManager())
        result = g.generate(cfg)
    """

    def __init__(self, path_manager: PathManager):
        self.pm = path_manager
        self.data_dirs = self.pm.ensure_data_dirs()
        self.dicts_dir = Path(self.data_dirs["dictionaries"])
        self.output_dir = Path(self.data_dirs["output"])

    # ==================================================================
    # 对外主接口
    # ==================================================================
    def generate(self, cfg: GenConfig) -> GenResult:
        """
        根据配置生成字典，统一入口
        """
        import time
        start_ts = time.time()

        # 1. 参数校验
        valid, err = self._validate(cfg)
        if not valid:
            return GenResult(False, None, 0, 0, err)

        # 2. 确保输出目录存在
        out_path = Path(cfg.output_file)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return GenResult(False, None, 0, 0,
                             f"创建输出目录失败: {type(exc).__name__}: {exc}")

        # 3. 分派到各模式生成器
        try:
            seen: Optional[set] = set() if cfg.remove_duplicates else None
            total_count = 0
            with open(out_path, "w", encoding=cfg.encoding, newline="") as fp:
                for line in self._iter_lines(cfg):
                    if not line:
                        continue
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    if cfg.remove_duplicates:
                        if line_stripped in seen:  # type: ignore[operator]
                            continue
                        seen.add(line_stripped)  # type: ignore[union-attr]
                    fp.write(line_stripped)
                    fp.write(cfg.line_ending)
                    total_count += 1
                    # 生成数量限制:达到指定行数立即停止(去重后计数)
                    if cfg.max_lines > 0 and total_count >= cfg.max_lines:
                        break
        except NotImplementedError as exc:
            return GenResult(False, None, 0, 0, f"[骨架阶段暂未实现] {exc}")
        except Exception as exc:  # noqa: BLE001
            return GenResult(False, None, 0, 0,
                             f"生成异常: {type(exc).__name__}: {exc}")

        # 4. 返回统计
        size = out_path.stat().st_size if out_path.exists() else 0
        return GenResult(
            success=True,
            output_file=str(out_path.resolve()),
            total_lines=total_count,
            size_bytes=size,
            duration_seconds=time.time() - start_ts,
        )

    def list_available_rules(self) -> List[str]:
        """返回当前已实现的规则名称列表（供UI下拉框使用）"""
        return sorted(_RULE_FUNCTIONS.keys())

    # ==================================================================
    # 对外辅助接口:预估生成结果(行数,字节数)
    # 用于生成前磁盘空间检查和"数量过多"提示
    # 预估值为去重前上界(偏高),磁盘检查更安全
    # ==================================================================
    def estimate(self, cfg: GenConfig) -> tuple:
        """预估生成结果
        :param cfg: 生成配置(会读取 max_lines 进行截断预估)
        :return: (estimated_lines, estimated_bytes)
            - estimated_lines: 预估行数(max_lines 截断后)
            - estimated_bytes: 预估输出文件字节数(按编码估算)
        注意:去重会减少实际行数,预估值是上界;不足 max_lines 时按实际数量预估
        """
        if cfg.mode == GenMode.CHARSET_COMB and cfg.charset:
            return self._estimate_charset_comb(cfg)
        elif cfg.mode == GenMode.PRESET:
            return self._estimate_preset(cfg)
        elif cfg.mode == GenMode.RULE:
            return self._estimate_rule(cfg)
        elif cfg.mode == GenMode.MASK and cfg.mask:
            return self._estimate_mask(cfg)
        else:
            return 0, 0

    def _avg_char_bytes(self, charset: str, encoding: str) -> float:
        """计算字符集在指定编码下的平均字节长度
        例:ASCII 字符集 UTF-8 编码平均=1.0;含中文则=3.0
        """
        if not charset:
            return 1.0
        try:
            total = sum(len(c.encode(encoding)) for c in charset)
            return total / len(charset)
        except Exception:  # noqa: BLE001
            return 1.0

    def _estimate_charset_comb(self, cfg: GenConfig) -> tuple:
        """预估字符集笛卡尔积模式的行数和字节数
        按长度从 min 到 max 顺序累加,模拟实际生成顺序,支持 max_lines 截断
        """
        charset_len = len(cfg.charset)  # type: ignore[arg-type]
        avg_bytes = self._avg_char_bytes(cfg.charset, cfg.encoding)  # type: ignore[arg-type]
        eol_bytes = len(cfg.line_ending.encode(cfg.encoding, errors="replace"))
        max_lines = cfg.max_lines
        total_lines = 0
        total_bytes = 0
        for length in range(cfg.min_length, cfg.max_length + 1):
            cnt = charset_len ** length
            # 单行字节数 = 字符平均字节 * 长度 + 换行符字节
            line_bytes = int(avg_bytes * length) + eol_bytes
            if max_lines > 0 and total_lines + cnt > max_lines:
                # 达到 max_lines 截断:只累加剩余行数
                remain = max_lines - total_lines
                total_lines = max_lines
                total_bytes += remain * line_bytes
                break
            total_lines += cnt
            total_bytes += cnt * line_bytes
        return total_lines, total_bytes

    def _estimate_preset(self, cfg: GenConfig) -> tuple:
        """预估内置弱口令+外部字典合并模式的行数和字节数"""
        eol_bytes = len(cfg.line_ending.encode(cfg.encoding, errors="replace"))
        lines: List[str] = []
        if cfg.include_builtin_weak:
            lines.extend(DEFAULT_WEAK_PASSWORDS)
        for f in cfg.extra_dict_files:
            p = Path(f)
            if not p.exists():
                continue
            try:
                with open(p, "r", encoding=cfg.encoding, errors="replace") as fp:
                    lines.extend(ln.strip() for ln in fp if ln.strip())
            except Exception:  # noqa: BLE001
                continue
        # max_lines 截断
        if cfg.max_lines > 0 and len(lines) > cfg.max_lines:
            lines = lines[:cfg.max_lines]
        total_bytes = sum(len(ln.encode(cfg.encoding, errors="replace")) + eol_bytes for ln in lines)
        return len(lines), total_bytes

    def _estimate_rule(self, cfg: GenConfig) -> tuple:
        """预估规则变异模式的行数和字节数(粗略估计)
        规则产出数量难以精确预估,这里按 base_dict 行数 * 规则数 * 平均产出估算
        """
        eol_bytes = len(cfg.line_ending.encode(cfg.encoding, errors="replace"))
        if not cfg.base_dict_file or not Path(cfg.base_dict_file).exists():
            return 0, 0
        try:
            with open(cfg.base_dict_file, "r", encoding=cfg.encoding, errors="replace") as fp:
                base_lines = [ln.strip() for ln in fp if ln.strip()]
        except Exception:  # noqa: BLE001
            return 0, 0
        base_count = len(base_lines)
        rule_count = len(cfg.rule_names) if cfg.rule_names else 1
        # 规则平均产出:noop=1, suffix_num_00_99=100, suffix_year=31 等,取保守均值 50
        avg_rule_yield = 50
        est_lines = base_count * rule_count * avg_rule_yield
        if cfg.max_lines > 0 and est_lines > cfg.max_lines:
            est_lines = cfg.max_lines
        # 平均行长:base 行平均长度 + 3(后缀)
        avg_base_len = sum(len(ln) for ln in base_lines) / base_count if base_count else 0
        avg_bytes = (avg_base_len + 3)
        est_bytes = int(est_lines * (avg_bytes + eol_bytes))
        return est_lines, est_bytes

    def _estimate_mask(self, cfg: GenConfig) -> tuple:
        """预估掩码模式的行数和字节数
        解析掩码,计算可变位置笛卡尔积总数 + 字面字符固定
        :param cfg: 含 mask 字段的生成配置
        :return: (estimated_lines, estimated_bytes)
        """
        eol_bytes = len(cfg.line_ending.encode(cfg.encoding, errors="replace"))
        try:
            tokens = self._parse_mask(cfg.mask)  # type: ignore[arg-type]
        except ValueError:
            return 0, 0
        # 可变位置数 = 各占位符字符集长度之积
        total_lines = 1
        mask_length = 0
        avg_char_bytes = 1.0  # 掩码默认 ASCII,平均1字节
        for is_placeholder, tok in tokens:
            mask_length += 1
            if is_placeholder and tok in MASK_MAP:
                total_lines *= len(MASK_MAP[tok])
        # max_lines 截断
        if cfg.max_lines > 0 and total_lines > cfg.max_lines:
            total_lines = cfg.max_lines
        # 每行字节数 = 掩码长度 * 平均字节 + 换行符
        line_bytes = int(avg_char_bytes * mask_length) + eol_bytes
        est_bytes = total_lines * line_bytes
        return total_lines, est_bytes

    # ==================================================================
    # 对外主接口:社工字典生成
    # ==================================================================
    def generate_social(self, sc: SocialConfig) -> GenResult:
        """根据目标信息生成社工字典
        :param sc: 社工配置(所有字段可选,填哪些用哪些)
        :return: GenResult
        流程:
            1. 校验输出路径
            2. 收集所有非空信息字段
            3. 调用 _iter_social 生成候选行
            4. 去重写入文件
        """
        import time
        start_ts = time.time()

        # 1. 参数校验
        if not sc.output_file:
            return GenResult(False, None, 0, 0, "output_file 不可为空")
        # 至少填一个信息字段(否则没东西可组合)
        info_fields = [
            sc.name_cn, sc.name_pinyin, sc.name_en, sc.nickname,
            sc.birth_year, sc.birth_month, sc.birth_day, sc.birth_full,
            sc.phone, sc.qq, sc.wechat, sc.email, sc.id_card,
            sc.company, sc.position, sc.employee_id, sc.school, sc.school_year,
            sc.spouse_name, sc.child_name, sc.pet_name, sc.anniversary, sc.car_plate,
            sc.favorite_words, sc.lucky_numbers, sc.area_code, sc.common_suffixes,
        ]
        if not any(f.strip() for f in info_fields):
            return GenResult(False, None, 0, 0, "至少填写一个目标信息字段")

        # 2. 确保输出目录存在
        out_path = Path(sc.output_file)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return GenResult(False, None, 0, 0,
                             f"创建输出目录失败: {type(exc).__name__}: {exc}")

        # 3. 生成并写入
        try:
            seen: Optional[set] = set() if sc.remove_duplicates else None
            total_count = 0
            with open(out_path, "w", encoding=sc.encoding, newline="") as fp:
                for line in self._iter_social(sc):
                    if not line:
                        continue
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    if sc.remove_duplicates:
                        if line_stripped in seen:  # type: ignore[operator]
                            continue
                        seen.add(line_stripped)  # type: ignore[union-attr]
                    fp.write(line_stripped)
                    fp.write(sc.line_ending)
                    total_count += 1
        except Exception as exc:  # noqa: BLE001
            return GenResult(False, None, 0, 0,
                             f"生成异常: {type(exc).__name__}: {exc}")

        # 4. 返回统计
        size = out_path.stat().st_size if out_path.exists() else 0
        return GenResult(
            success=True,
            output_file=str(out_path.resolve()),
            total_lines=total_count,
            size_bytes=size,
            duration_seconds=time.time() - start_ts,
        )

    # ==================================================================
    # 内部：参数校验
    # ==================================================================
    def _validate(self, cfg: GenConfig) -> (bool, Optional[str]):
        if not cfg.output_file:
            return False, "output_file 不可为空"
        if cfg.mode == GenMode.CHARSET_COMB:
            if not cfg.charset:
                return False, "CHARSET_COMB 模式必须指定 charset"
            if cfg.min_length <= 0 or cfg.max_length < cfg.min_length:
                return False, "CHARSET_COMB: min_length/max_length 不合法"
            charset_len = len(cfg.charset)
            total_combos = 0
            for L in range(cfg.min_length, cfg.max_length + 1):
                total_combos += charset_len ** L
            # 安全阈值：超过 50 亿候选先拦下来（避免把机器跑死）
            # 超过这个量级写中间字典会撑爆磁盘,应改走 hashcat -a 3 掩码模式
            if total_combos > 5_000_000_000:
                return False, (
                    f"组合数量过高({total_combos:,})，建议改走:密码破解 > 掩码模式(-a 3)。"
                    "或调小长度范围/字符集后重试。"
                )
        if cfg.mode == GenMode.MASK:
            if not cfg.mask:
                return False, "MASK 模式必须指定 mask"
            # 校验掩码占位符合法性 + 组合数上限
            try:
                tokens = self._parse_mask(cfg.mask)
            except ValueError as exc:
                return False, str(exc)
            # 计算组合数:各可变位置字符集长度之积
            mask_combos = 1
            for is_placeholder, tok in tokens:
                if is_placeholder and tok in MASK_MAP:
                    mask_combos *= len(MASK_MAP[tok])
            # 掩码组合数上限:同 CHARSET_COMB,超过 50 亿拒绝(应改走 hashcat -a 3)
            if mask_combos > 5_000_000_000:
                return False, (
                    f"掩码组合数量过高({mask_combos:,}),建议直接用 Hashcat -a 3 执行,"
                    f"或减少占位符数量后重试。"
                )
        if cfg.mode == GenMode.RULE:
            if not cfg.base_dict_file or not Path(cfg.base_dict_file).exists():
                return False, "RULE 模式必须指定存在的 base_dict_file"
            for r in cfg.rule_names:
                if r not in _RULE_FUNCTIONS:
                    return False, f"未知规则名: {r}，请用 list_available_rules() 查询"
        return True, None

    # ==================================================================
    # 内部：各行生成迭代器（骨架阶段）
    # ==================================================================
    def _iter_lines(self, cfg: GenConfig) -> Iterable[str]:
        """
        按模式生成候选行，逐行 yield（流式避免一次性吃内存）
        """
        if cfg.mode == GenMode.PRESET:
            yield from self._iter_preset(cfg)
        elif cfg.mode == GenMode.CHARSET_COMB:
            yield from self._iter_charset(cfg)
        elif cfg.mode == GenMode.MASK:
            yield from self._iter_mask(cfg)
        elif cfg.mode == GenMode.RULE:
            yield from self._iter_rule(cfg)
        else:
            raise ValueError(f"未知生成模式: {cfg.mode}")

    # ---------------- 各模式实现 ----------------
    def _iter_preset(self, cfg: GenConfig) -> Iterable[str]:
        """模式1：内置弱口令 + 外部字典合并"""
        if cfg.include_builtin_weak:
            for pw in DEFAULT_WEAK_PASSWORDS:
                yield pw
        for f in cfg.extra_dict_files:
            p = Path(f)
            if not p.exists():
                continue
            with open(p, "r", encoding=cfg.encoding, errors="replace") as fp:
                for ln in fp:
                    yield ln

    def _iter_charset(self, cfg: GenConfig) -> Iterable[str]:
        """模式2：字符集笛卡尔积（骨架阶段已实现，基础功能可用）"""
        assert cfg.charset is not None
        for length in range(cfg.min_length, cfg.max_length + 1):
            for combo in itertools.product(cfg.charset, repeat=length):
                yield "".join(combo)

    def _iter_mask(self, cfg: GenConfig) -> Iterable[str]:
        """模式3:Hashcat 兼容掩码生成
        解析掩码字符串(如 ?d?d?d?d 或 pass?l?l),按每个位置的字符集做笛卡尔积
        支持的占位符:?l ?u ?d ?s ?a ?b(见 MASK_MAP)
        支持字面字符混合:如 pass?d?d?d → pass000~pass999
        :param cfg: 含 mask 字段的生成配置
        :yield: 候选密码字符串
        """
        assert cfg.mask is not None
        # 解析掩码:拆成 token 列表,每个 token 为 (charset_str,) 或 (literal_char,)
        # 例: "?d?d?d?d" → [("?d",), ("?d",), ("?d",), ("?d",)]
        # 例: "pass?d?d" → [("p",), ("a",), ("s",), ("s",), ("?d",), ("?d",)]
        tokens = self._parse_mask(cfg.mask)
        # 分离:可变位置(占位符) + 固定位置(字面字符)
        # 可变位置做笛卡尔积,固定位置直接插入
        variableCharsets = []  # [(index_in_token_list, charset_str), ...]
        for idx, tok in enumerate(tokens):
            if tok[1] in MASK_MAP:
                variableCharsets.append((idx, MASK_MAP[tok[1]]))
            else:
                # 字面字符,token[1] 就是字符本身
                tokens[idx] = (False, tok[1])
        # 笛卡尔积:仅对可变位置
        variableSets = [cs for _, cs in variableCharsets]
        variableIndices = [idx for idx, _ in variableCharsets]
        for combo in itertools.product(*variableSets):
            # combo 是各可变位置的字符元组,按顺序填回 tokens
            result_chars = []
            combo_ptr = 0
            for idx, tok in enumerate(tokens):
                if idx in variableIndices:
                    result_chars.append(combo[combo_ptr])
                    combo_ptr += 1
                else:
                    result_chars.append(tok[1])
            yield "".join(result_chars)

    @staticmethod
    def _parse_mask(mask: str) -> list:
        """解析掩码字符串为 token 列表
        :param mask: 掩码字符串,如 "?d?d?d?d" 或 "pass?l?l" 或 "a?db"
        :return: [(is_placeholder, token_str), ...]
            - is_placeholder=True 时 token_str 为 "?d" 等(在 MASK_MAP 中)
            - is_placeholder=False 时 token_str 为字面字符
        :raises ValueError: 遇到未知占位符(如 ?x)时抛出
        """
        tokens = []
        i = 0
        n = len(mask)
        while i < n:
            ch = mask[i]
            if ch == "?" and i + 1 < n:
                placeholder = mask[i:i + 2]  # 如 "?d"
                if placeholder in MASK_MAP:
                    tokens.append((True, placeholder))
                    i += 2
                    continue
                else:
                    raise ValueError(
                        f"未知掩码占位符: {placeholder}"
                        f"(支持: ?l ?u ?d ?s ?a ?b)"
                    )
            # 字面字符(包括单独的 ? 在末尾)
            tokens.append((False, ch))
            i += 1
        return tokens

    def _iter_rule(self, cfg: GenConfig) -> Iterable[str]:
        """模式4：基础字典 + 规则变异（骨架阶段已实现基础规则）"""
        assert cfg.base_dict_file is not None
        with open(cfg.base_dict_file, "r", encoding=cfg.encoding, errors="replace") as fp:
            for raw in fp:
                base = raw.strip()
                if not base:
                    continue
                for rule_name in cfg.rule_names or ["noop"]:
                    func = _RULE_FUNCTIONS[rule_name]
                    for mutated in func(base):
                        yield mutated

    # ==================================================================
    # 内部:社工字典生成迭代器
    # 策略:收集所有非空信息字段 → 提取基础token → 笛卡尔积组合 + 后缀变异
    # ==================================================================
    def _iter_social(self, sc: SocialConfig) -> Iterable[str]:
        """社工字典生成:基于目标信息自动组合变异
        生成层次:
            1. 原始token(姓名/生日/手机等直接作为密码)
            2. 两两组合(name+123, phone+name 等)
            3. 常见后缀变异(@123, #123, 123, 123! 等)
            4. 生日组合(name+年, name+年月日, name+月日 等)
            5. 身份证后6位 + 姓名组合
            6. 手机尾号 + 姓名组合
        所有字段可选,填哪些用哪些,空字段自动跳过
        """
        # -------- 第1步:收集基础token --------
        tokens: List[str] = []

        # 姓名类(中文/拼音/英文/昵称)
        for name in (sc.name_cn, sc.name_pinyin, sc.name_en, sc.nickname):
            if name.strip():
                n = name.strip()
                # 拼音处理:分写时去空格作为 token(如 "zhang san" → zhangsan)
                # 仅对纯英文字母生效,中文字符保留原值
                # 注意:含空格的字符串 isalpha() 返回 False,需先去空格判断
                if n.isascii() and n.replace(" ", "").isalpha() and " " in n:
                    parts = [p for p in n.split() if p]
                    joined = "".join(parts)
                    tokens.append(joined)
                    # 首字母组合(如 zhang san → zs)
                    if len(parts) >= 2:
                        initials = "".join(p[0] for p in parts)
                        if initials and initials != joined:
                            tokens.append(initials)
                else:
                    tokens.append(n)

        # 生日类(年/月/日/完整)
        birth_parts: List[str] = []
        for b in (sc.birth_year, sc.birth_month, sc.birth_day, sc.birth_full):
            if b.strip():
                b_clean = b.strip()
                tokens.append(b_clean)
                birth_parts.append(b_clean)
                # 月份/日期去前导零(如 01 → 1)
                if b_clean.startswith("0") and len(b_clean) > 1:
                    tokens.append(b_clean.lstrip("0"))

        # 数字类(手机/QQ/工号/身份证/车牌/纪念日/入学年份/区号)
        for num in (sc.phone, sc.qq, sc.employee_id, sc.id_card,
                    sc.car_plate, sc.anniversary, sc.school_year, sc.area_code):
            if num.strip():
                tokens.append(num.strip())
                # 手机号尾号(后4位/后6位)
                if num.strip().isdigit() and len(num.strip()) >= 6:
                    tokens.append(num.strip()[-4:])
                    tokens.append(num.strip()[-6:])

        # 微信号(整体作为 token,常含字母数字下划线,不做尾号拆分)
        if sc.wechat.strip():
            tokens.append(sc.wechat.strip())

        # 邮箱(取@前部分)
        if sc.email.strip():
            email_prefix = sc.email.strip().split("@")[0]
            if email_prefix:
                tokens.append(email_prefix)

        # 身份证后6位(单独提取,常用密码)
        if sc.id_card.strip() and len(sc.id_card.strip()) >= 6:
            tokens.append(sc.id_card.strip()[-6:])

        # 公司/职位/学校/配偶/子女/宠物(中文也作为token)
        for word in (sc.company, sc.position, sc.school,
                     sc.spouse_name, sc.child_name, sc.pet_name):
            if word.strip():
                tokens.append(word.strip())

        # 喜好词汇/幸运数字(逗号分隔,拆分)
        for csv_field in (sc.favorite_words, sc.lucky_numbers):
            if csv_field.strip():
                for part in csv_field.split(","):
                    part = part.strip()
                    if part:
                        tokens.append(part)

        # 去重token(保持顺序)
        seen_token: set = set()
        unique_tokens: List[str] = []
        for t in tokens:
            if t not in seen_token:
                seen_token.add(t)
                unique_tokens.append(t)

        # -------- 第2步:收集后缀 --------
        suffixes: List[str] = ["", "123", "1234", "123456", "888", "666",
                                "@123", "#123", "!@#", "!@#$", "@", "#", "!"]
        # 用户自定义后缀
        if sc.common_suffixes.strip():
            for part in sc.common_suffixes.split(","):
                part = part.strip()
                if part:
                    suffixes.append(part)
        # 幸运数字作为后缀
        if sc.lucky_numbers.strip():
            for part in sc.lucky_numbers.split(","):
                part = part.strip()
                if part:
                    suffixes.append(part)
                    suffixes.append(part + part)  # 如 6 → 66

        # -------- 第3步:生成候选 --------
        # 层次1:原始token + 后缀
        for token in unique_tokens:
            for suffix in suffixes:
                yield token + suffix

        # 层次2:姓名 × 生日组合(高频密码模式)
        # 收集所有姓名类token(包括去空格后的拼音)
        name_set = set()
        for name in (sc.name_cn, sc.name_pinyin, sc.name_en, sc.nickname,
                     sc.spouse_name, sc.child_name, sc.pet_name):
            if name.strip():
                name_set.add(name.strip())
                # 拼音分写时,去空格版本也在 tokens 中,需一并加入 name_set
                if name.strip().isascii() and " " in name.strip():
                    name_set.add(name.strip().replace(" ", ""))
        name_tokens = [t for t in unique_tokens if t in name_set]
        for name in name_tokens:
            for birth in birth_parts:
                yield name + birth
                yield birth + name
            # 姓名 × 手机尾号
            if sc.phone.strip() and len(sc.phone.strip()) >= 4:
                yield name + sc.phone.strip()[-4:]
                yield name + sc.phone.strip()[-6:]
            # 姓名 × 身份证后6位
            if sc.id_card.strip() and len(sc.id_card.strip()) >= 6:
                yield name + sc.id_card.strip()[-6:]
            # 姓名 × QQ
            if sc.qq.strip():
                yield name + sc.qq.strip()

        # 层次3:生日格式变换(19900115 → 900115, 1990-01-15, 01-15 等)
        if sc.birth_full.strip():
            bf = sc.birth_full.strip()
            yield bf
            # 去分隔符
            bf_clean = bf.replace("-", "").replace("/", "").replace(".", "")
            if bf_clean != bf:
                yield bf_clean
            # 取后6位(900115)
            if len(bf_clean) >= 6:
                yield bf_clean[-6:]
            # 年-月-日 格式
            if len(bf_clean) == 8:
                yield f"{bf_clean[:4]}-{bf_clean[4:6]}-{bf_clean[6:8]}"
                yield f"{bf_clean[4:6]}-{bf_clean[6:8]}"
                yield f"{bf_clean[:4]}{bf_clean[4:6]}"

        # 层次4:纯数字组合(手机号/QQ/工号/身份证后6位单独输出)
        for num in (sc.phone, sc.qq, sc.employee_id):
            if num.strip():
                yield num.strip()
                # + 常见后缀
                for suffix in ("", "123", "888", "666", "@123"):
                    yield num.strip() + suffix

        # 层次5~16:高级组合(姓名×其他字段/家庭成员组合/身份证生日/大小写变换等)
        # 单独抽到 _iter_social_advanced,保持本方法可读性
        # 注意:社工字典不再包含 DEFAULT_WEAK_PASSWORDS(123456/password 等弱口令)
        # 这些与目标人物信息无关,属于经典字典范畴,用经典字典生成即可
        yield from self._iter_social_advanced(
            sc, unique_tokens, birth_parts, name_tokens
        )

    # ==================================================================
    # 内部:社工字典高级组合层次
    # 在基础层次(原始token/姓名×生日/纯数字)之上,扩展更多社工常见密码模式
    # 所有字段可选,空字段自动跳过,不会产生空候选
    # ==================================================================
    def _iter_social_advanced(
        self,
        sc: SocialConfig,
        unique_tokens: List[str],
        birth_parts: List[str],
        name_tokens: List[str],
    ) -> Iterable[str]:
        """社工字典高级组合层次
        :param sc: 社工配置
        :param unique_tokens: 基础层已去重的 token 列表(姓名/生日/数字等)
        :param birth_parts: 生日相关字段(年/月/日/完整)
        :param name_tokens: 姓名类 token(含去空格拼音/配偶/子女/宠物)
        :return: 候选密码迭代器
        覆盖层次:
            5.  姓名 × 其他信息字段(公司/学校/职位/工号/邮箱/纪念日/车牌/区号/喜好词)
            6.  配偶/子女/宠物名 × 生日/手机尾号
            7.  姓名 × 配偶/子女/宠物名(互相组合)
            8.  身份证提取出生日期(18位第7-14位)及格式变换
            9.  英文/拼音姓名大小写变换(ZHANGSAN/Zhangsan) + 生日
            10. 手机号中间4位 + 姓名
            11. 生日倒序 + 姓名
            12. 车牌号去汉字部分(保留字母数字)
            13. 邮箱前缀 + 数字后缀
            14. 纪念日格式变换 + 姓名
            15. 键盘序列 + 常见英文词
            16. 公司/学校英文名 + 数字
        """
        # -------- 层次5:姓名 × 其他信息字段 --------
        # 公司/学校/职位/工号/纪念日/车牌/区号/喜好词/幸运数字/微信号
        other_fields: List[str] = []
        for field_val in (
            sc.company, sc.school, sc.position, sc.employee_id,
            sc.anniversary, sc.car_plate, sc.area_code, sc.wechat,
        ):
            if field_val.strip():
                other_fields.append(field_val.strip())
        # 喜好词汇/幸运数字(逗号分隔拆分)
        for csv_field in (sc.favorite_words, sc.lucky_numbers):
            if csv_field.strip():
                for part in csv_field.split(","):
                    part = part.strip()
                    if part:
                        other_fields.append(part)
        # 邮箱前缀作为可组合字段
        if sc.email.strip():
            email_prefix = sc.email.strip().split("@")[0]
            if email_prefix:
                other_fields.append(email_prefix)

        for name in name_tokens:
            for field_val in other_fields:
                # 过滤纯中文×纯中文组合(如 张三+腾讯/工程师)
                # 这类组合作为压缩包密码命中率极低,纯属噪音
                if _is_pure_cjk(name) and _is_pure_cjk(field_val):
                    continue
                yield name + field_val
                yield field_val + name

        # -------- 层次6:配偶/子女/宠物名 × 生日/手机尾号 --------
        family_names: List[str] = []
        for fname in (sc.spouse_name, sc.child_name, sc.pet_name):
            if fname.strip():
                family_names.append(fname.strip())
        for fname in family_names:
            # × 生日(正反两种顺序)
            for birth in birth_parts:
                yield fname + birth
                yield birth + fname
            # × 手机尾号(后4位/后6位)
            if sc.phone.strip() and len(sc.phone.strip()) >= 4:
                yield fname + sc.phone.strip()[-4:]
                yield fname + sc.phone.strip()[-6:]
            # × 身份证后6位
            if sc.id_card.strip() and len(sc.id_card.strip()) >= 6:
                yield fname + sc.id_card.strip()[-6:]
            # × QQ
            if sc.qq.strip():
                yield fname + sc.qq.strip()

        # -------- 层次7:姓名 × 配偶/子女/宠物名(互相组合) --------
        for name in name_tokens:
            for fname in family_names:
                if name != fname:
                    # 过滤纯中文×纯中文组合(如 张三+李四/旺财)
                    # 这类组合作为压缩包密码命中率极低
                    if _is_pure_cjk(name) and _is_pure_cjk(fname):
                        continue
                    yield name + fname
                    yield fname + name

        # -------- 层次8:身份证提取出生日期 --------
        # 18位身份证第7-14位为出生日期(YYYYMMDD),是高频密码来源
        birth_from_id: Optional[str] = None
        if sc.id_card.strip():
            id_clean = sc.id_card.strip()
            if len(id_clean) == 18 and id_clean[6:14].isdigit():
                birth_from_id = id_clean[6:14]
                yield birth_from_id
                # 后6位(出生月日+顺序位)
                yield birth_from_id[-6:]
                # 年-月-日 格式
                yield f"{birth_from_id[:4]}-{birth_from_id[4:6]}-{birth_from_id[6:8]}"
                # 月-日
                yield f"{birth_from_id[4:6]}-{birth_from_id[6:8]}"
                # 仅年
                yield birth_from_id[:4]
                # 仅月日
                yield birth_from_id[4:8]
                # 补入 birth_parts 供后续姓名组合使用
                if birth_from_id not in birth_parts:
                    birth_parts.append(birth_from_id)
                    birth_parts.append(birth_from_id[-6:])

        # -------- 层次9:英文/拼音姓名大小写变换 + 生日 --------
        # 中文姓名不适用大小写变换,仅对 ASCII 姓名(拼音/英文)处理
        for name in (sc.name_pinyin, sc.name_en, sc.nickname):
            if name.strip() and name.strip().isascii():
                n = name.strip().replace(" ", "")
                if n:
                    yield n.upper()           # ZHANGSAN
                    yield n.capitalize()       # Zhangsan
                    # 大小写 × 生日
                    for birth in birth_parts:
                        yield n.upper() + birth
                        yield n.capitalize() + birth
                    # 大小写 × 手机尾号
                    if sc.phone.strip() and len(sc.phone.strip()) >= 4:
                        yield n.upper() + sc.phone.strip()[-4:]
                        yield n.capitalize() + sc.phone.strip()[-4:]

        # -------- 层次10:手机号中间4位 + 姓名 --------
        # 11位手机号第4-7位为中间4位,部分用户用作密码
        if sc.phone.strip() and len(sc.phone.strip()) == 11:
            mid4 = sc.phone.strip()[3:7]
            yield mid4
            for name in name_tokens:
                yield name + mid4
                yield mid4 + name

        # -------- 层次11:生日倒序 + 姓名 --------
        # 19900115 → 51100991,部分用户用倒序生日作密码
        if sc.birth_full.strip():
            bf_clean = (
                sc.birth_full.strip()
                .replace("-", "").replace("/", "").replace(".", "")
            )
            if len(bf_clean) == 8 and bf_clean.isdigit():
                reversed_bf = bf_clean[::-1]
                yield reversed_bf
                for name in name_tokens:
                    yield name + reversed_bf
                    yield reversed_bf + name
        # 身份证提取的生日也做倒序
        if birth_from_id and len(birth_from_id) == 8:
            reversed_id_bf = birth_from_id[::-1]
            yield reversed_id_bf
            for name in name_tokens:
                yield name + reversed_id_bf

        # -------- 层次12:车牌号去汉字部分(保留字母数字) --------
        # 京A12345 → A12345,汉字省份前缀去掉后是常用密码片段
        if sc.car_plate.strip():
            plate = sc.car_plate.strip()
            plate_an = "".join(c for c in plate if c.isascii() and c.isalnum())
            if plate_an and plate_an != plate:
                yield plate_an
                for name in name_tokens:
                    yield name + plate_an
                    yield plate_an + name

        # -------- 层次13:邮箱前缀 + 数字后缀 --------
        if sc.email.strip():
            email_prefix = sc.email.strip().split("@")[0]
            if email_prefix:
                for suffix in ("", "123", "123456", "888", "666", "@123"):
                    yield email_prefix + suffix
                # 邮箱前缀 × 姓名
                for name in name_tokens:
                    yield name + email_prefix
                    yield email_prefix + name

        # -------- 层次14:纪念日格式变换 + 姓名 --------
        if sc.anniversary.strip():
            ann = sc.anniversary.strip()
            ann_clean = ann.replace("-", "").replace("/", "").replace(".", "")
            yield ann_clean
            if len(ann_clean) == 8 and ann_clean.isdigit():
                yield f"{ann_clean[:4]}-{ann_clean[4:6]}-{ann_clean[6:8]}"
                yield ann_clean[-6:]
                yield ann_clean[::-1]
            for name in name_tokens:
                yield name + ann_clean
                yield ann_clean + name

        # -------- 层次15:键盘序列 + 常见英文词 --------
        # 键盘相邻键序列与常见英文词,社工场景命中率较高
        # 注意:只产出「键盘序列/常见词 + 后缀」「键盘序列/常见词 × 姓名」这类变体;
        #   原词(qwerty/admin/password等已被经典字典覆盖)不再独立产出,避免弱密码混入
        keyboard_seqs = [
            "qwerty", "asdfgh", "zxcvbn", "1qaz2wsx",
            "1234qwer", "qwer1234", "asdf1234",
            "qweasdzxc", "1234567890",
        ]
        common_words = [
            "love", "admin", "root", "pass", "welcome",
            "hello", "master", "super", "test", "user",
        ]
        weak_strip = set(DEFAULT_WEAK_PASSWORDS)
        for seq in keyboard_seqs:
            # 过滤:seq 本身若在弱口令表(如 qwerty)中,不再裸产出,只产带后缀或拼姓名的
            if seq not in weak_strip:
                yield seq
            for suffix in ("123", "123456", "888", "@123", "#123", "666"):
                cand = seq + suffix
                if cand not in weak_strip:
                    yield cand
            # 键盘序列 × 姓名
            for name in name_tokens:
                yield name + seq
                yield seq + name
        for word in common_words:
            # 原词若是弱口令(admin),跳过裸词
            if word not in weak_strip:
                yield word
            for suffix in ("123", "123456", "888", "666", "@123", "#123", "520"):
                cand = word + suffix
                if cand not in weak_strip:
                    yield cand
            # 常见英文词 × 姓名
            for name in name_tokens:
                yield name + word
                yield word + name

        # -------- 层次16:公司/学校英文名 + 数字 --------
        # 仅对 ASCII 名称处理(中文公司名在层次5已作为 token 组合)
        for org_name in (sc.company, sc.school):
            if org_name.strip() and org_name.strip().isascii():
                org_lower = org_name.strip().lower()
                yield org_lower
                for suffix in ("", "123", "123456", "888", "666", "@123"):
                    yield org_lower + suffix
                # 公司名 × 姓名
                for name in name_tokens:
                    yield name + org_lower
                    yield org_lower + name

        # -------- 层次17:姓名类 token + 后缀(大小写变种) --------
        # 对 ASCII 姓名(拼音/英文/昵称)增加:首字母大写、全大写 + 常见后缀
        # 例: zhangsan → Zhangsan / ZHANGSAN → Zhangsan1990 / ZHANGSAN@123
        extra_suffixes = ["", "123", "123456", "888", "666", "@123", "#123", "!@#", "1234", "520"]
        for name in (sc.name_pinyin, sc.name_en, sc.nickname):
            if name.strip() and name.strip().isascii():
                n = name.strip().replace(" ", "")
                if not n:
                    continue
                variants = [n, n.upper(), n.capitalize()]
                for nv in variants:
                    if nv == n:
                        # 原始小写在层次1 suffixes 已 yield,跳过避免重复
                        continue
                    for suf in extra_suffixes:
                        yield nv + suf
                    # 大小写变种 × 生日
                    for birth in birth_parts:
                        yield nv + birth
                        yield birth + nv
                    # 大小写变种 × 手机尾号 / QQ / 微信号
                    if sc.phone.strip() and len(sc.phone.strip()) >= 4:
                        yield nv + sc.phone.strip()[-4:]
                        yield nv + sc.phone.strip()[-6:]
                    if sc.qq.strip():
                        yield nv + sc.qq.strip()
                    if sc.wechat.strip():
                        yield nv + sc.wechat.strip()
                        yield sc.wechat.strip() + nv

        # -------- 层次18:微信号 深度组合 --------
        # 微信号 + 后缀 + 与各数字字段正反拼接
        if sc.wechat.strip():
            wx = sc.wechat.strip()
            for suf in extra_suffixes:
                yield wx + suf
            # × 生日(正反)
            for birth in birth_parts:
                yield wx + birth
                yield birth + wx
            # × 手机尾号
            if sc.phone.strip() and len(sc.phone.strip()) >= 4:
                yield wx + sc.phone.strip()[-4:]
                yield wx + sc.phone.strip()[-6:]
            # × 姓名类(正反)
            for name in name_tokens:
                yield wx + name
                yield name + wx
            # 微信号首字母大写 + 后缀
            if wx.isascii():
                for suf in extra_suffixes:
                    yield wx.capitalize() + suf
                    yield wx.upper() + suf

        # -------- 层次19:姓名类首字母缩写(zs)与数字字段组合 --------
        # 对分写拼音、英文姓名、昵称,提取首字母组合,与生日/手机/QQ/微信号深度组合
        # 例: zhang san → zs → zs1990 / zs1380 / zs_wx / 1990zs / 1380zs
        initial_set: set = set()
        for name in (sc.name_pinyin, sc.name_en, sc.nickname):
            if not name.strip():
                continue
            n_clean = name.strip()
            if n_clean.isascii() and " " in n_clean:
                parts = [p for p in n_clean.split() if p]
                if len(parts) >= 2:
                    initials = "".join(p[0] for p in parts)
                    if initials:
                        initial_set.add(initials.lower())
                        initial_set.add(initials.upper())
        for ini in initial_set:
            # ini + 后缀
            for suf in extra_suffixes:
                yield ini + suf
            # ini × 生日(正反)
            for birth in birth_parts:
                yield ini + birth
                yield birth + ini
            # ini × 手机尾号
            if sc.phone.strip() and len(sc.phone.strip()) >= 4:
                yield ini + sc.phone.strip()[-4:]
                yield ini + sc.phone.strip()[-6:]
            # ini × QQ
            if sc.qq.strip():
                yield ini + sc.qq.strip()
                yield sc.qq.strip() + ini
            # ini × 微信号
            if sc.wechat.strip():
                yield ini + sc.wechat.strip()
                yield sc.wechat.strip() + ini
            # ini × 姓名类
            for name in name_tokens:
                if name != ini:
                    yield ini + name
                    yield name + ini
            # ini × 幸运数字
            if sc.lucky_numbers.strip():
                for luck in sc.lucky_numbers.split(","):
                    luck = luck.strip()
                    if luck:
                        yield ini + luck
                        yield luck + ini

        # -------- 层次20:姓名类 token 之间交叉(张三/zhangsan/zs/三哥互相拼) --------
        # 例:张三zhangsan / zhangsan张三 / 三哥zhangsan / zszhangsan
        name_dedup: list = []
        seen_nt: set = set()
        for nt in name_tokens:
            if nt not in seen_nt:
                seen_nt.add(nt)
                name_dedup.append(nt)
        for i in range(len(name_dedup)):
            for j in range(len(name_dedup)):
                if i == j:
                    continue
                a, b = name_dedup[i], name_dedup[j]
                # 过滤 纯中文 × 纯中文(如张三三哥),命中极低
                if _is_pure_cjk(a) and _is_pure_cjk(b):
                    continue
                yield a + b

        # -------- 层次21:手机号分段(前3+中4+后4 交叉) + 姓名 --------
        # 例:138 + 0013 + 8000 = 13800138000; 密码常见:1380013 / 00138000 / 138+张三
        if sc.phone.strip() and sc.phone.strip().isdigit() and len(sc.phone.strip()) == 11:
            ph = sc.phone.strip()
            seg3 = ph[:3]     # 号段前3位:138
            seg4m = ph[3:7]   # 中间4位:0013
            seg4e = ph[7:]    # 最后4位:8000
            seg7  = ph[:7]    # 前7位:1380013
            seg7s = ph[4:]    # 后7位:0138000
            seg_mid6 = ph[2:8]# 中间6位:800138
            phone_segs = [seg3, seg4m, seg4e, seg7, seg7s, seg_mid6]
            for seg in phone_segs:
                yield seg
                for suf in ("123", "888", "666", "@123"):
                    yield seg + suf
                for name in name_tokens:
                    yield seg + name
                    yield name + seg
            # 号段两两拼接:138+8000 / 0013+8000 / 138+0138 等
            for a in phone_segs:
                for b in phone_segs:
                    if a != b:
                        yield a + b

        # -------- 层次22:工号/学号/车牌号 与 姓名/生日 交叉 --------
        # 工号:T10086 → T10086张三 / 张三T10086 / T100861990
        for code_val, label in (
            (sc.employee_id, "employee"),
            (sc.school_year, "school_year"),
        ):
            if not code_val.strip():
                continue
            cv = code_val.strip()
            # code + 后缀
            for suf in ("123", "888", "666", "@123"):
                yield cv + suf
            # code × 姓名
            for name in name_tokens:
                yield cv + name
                yield name + cv
            # code × 生日
            for birth in birth_parts:
                yield cv + birth
                yield birth + cv
        # 车牌号(字母数字部分,层次12已去汉字,这里再拼一次姓名/生日)
        if sc.car_plate.strip():
            plate = sc.car_plate.strip()
            plate_an = "".join(c for c in plate if c.isascii() and c.isalnum())
            if plate_an:
                for name in name_tokens:
                    # 中文姓名过滤:车牌×中文(如 T10086张三)保留,反向(张三T10086)也保留
                    if not (_is_pure_cjk(name) and _is_pure_cjk(plate_an)):
                        yield name + plate_an
                for birth in birth_parts:
                    yield plate_an + birth

        # -------- 层次23:QQ号 深度组合 --------
        if sc.qq.strip() and sc.qq.strip().isdigit():
            qq = sc.qq.strip()
            for suf in ("123", "888", "666", "@123"):
                yield qq + suf
            # QQ × 生日(正反)
            for birth in birth_parts:
                yield qq + birth
                yield birth + qq
            # QQ 尾号(3~5位) + 姓名
            if len(qq) >= 5:
                qq_tails = [qq[-3:], qq[-4:], qq[-5:]]
                for tail in qq_tails:
                    yield tail
                    for name in name_tokens:
                        yield tail + name
                        yield name + tail

        # -------- 层次24:入学年份/年份类 与 姓名/学号/公司名 --------
        # 2008级 → 2008zhangsan / 200801(08级01班)
        for year_field in (sc.school_year, sc.birth_year):
            if year_field.strip() and year_field.strip().isdigit() and len(year_field.strip()) == 4:
                yf = year_field.strip()
                for suf in ("123", "888", "666", "@123"):
                    yield yf + suf
                # 2008 → 08(后两位)
                yy = yf[2:]
                for name in name_tokens:
                    yield yy + name
                    yield name + yy
                # 0801 ~ 0812(班级号,常见学号前缀)
                for cls in range(1, 13):
                    yield yf + f"{cls:02d}"
                    yield yy + f"{cls:02d}"

        # -------- 层次25:姓名 × 纪念日(正反) + 纪念日 + 后缀 --------
        # 纪念日:20151001 → 张三2015 / 1001张三 / 20151001@123
        if sc.anniversary.strip():
            ann_clean = sc.anniversary.strip().replace("-", "").replace("/", "").replace(".", "")
            if ann_clean.isdigit():
                for suf in ("123", "888", "666", "@123"):
                    yield ann_clean + suf
                # 取前后4位分别组合
                if len(ann_clean) >= 8:
                    y4 = ann_clean[:4]
                    md4 = ann_clean[4:]
                    for name in name_tokens:
                        yield y4 + name
                        yield md4 + name
                        yield name + md4


if __name__ == "__main__":
    import sys
    g = DictGenerator(PathManager())
    # 示例：生成 4 位纯数字字典 + 内置弱口令合并输出
    out = Path(__file__).resolve().parent.parent / "data" / "output" / "demo_dict.txt"
    if len(sys.argv) >= 2:
        out = Path(sys.argv[1])
    cfg = GenConfig(
        output_file=str(out),
        mode=GenMode.CHARSET_COMB,
        charset="0123456789",
        min_length=4,
        max_length=4,
    )
    r = g.generate(cfg)
    print(f"success      : {r.success}")
    print(f"output_file  : {r.output_file}")
    print(f"total_lines  : {r.total_lines:,}")
    print(f"size_bytes   : {r.size_bytes:,}")
    print(f"duration(s)  : {r.duration_seconds:.3f}")
    if r.error_message:
        print(f"error        : {r.error_message}")
