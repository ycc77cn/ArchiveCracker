# -*- coding: utf-8 -*-
"""
文件名称：archive_detector.py
功能描述：压缩包类型检测模块
         通过读取文件头部的「Magic Bytes / 魔数」判断压缩包的真实容器类型，
         而不是仅依靠文件扩展名，避免用户恶意修改扩展名导致后续流程选错提取工具。
         支持识别：ZIP (含分卷)、RAR3、RAR5、7-Zip。
创建日期：2026-08-03
修改记录：
    2026-08-03  初始版本：完成 ZIP/RAR3/RAR5/7Z 四种魔数的识别框架
"""

import os
from pathlib import Path
from enum import Enum
from typing import Optional, Tuple


class ArchiveType(Enum):
    """
    已支持的压缩包类型枚举
    说明：后续新增格式（如 PDF、Office）可在此扩展，并配套补充哈希提取器。
    """
    ZIP = "zip"            # ZIP / WinZip AES 加密
    RAR3 = "rar3"          # RAR3 旧版格式 (RAR 4.x)
    RAR5 = "rar5"          # RAR5 新版格式 (RAR 5.x+)
    SEVEN_Z = "7z"         # 7-Zip 格式
    UNKNOWN = "unknown"    # 未识别 / 不支持的格式


# 魔数对照表：(识别字节序列, ArchiveType, 类型名称)
# 注意：RAR3 与 RAR5 魔数不同，需分别处理；ZIP 分卷（.z01/.zip.001）与普通 ZIP 魔数相同。
_MAGIC_TABLE: Tuple[Tuple[bytes, ArchiveType, str], ...] = (
    # 7-Zip: 37 7A BC AF 27 1C
    (b"\x37\x7a\xbc\xaf\x27\x1c", ArchiveType.SEVEN_Z, "7-Zip"),
    # RAR5: 52 61 72 21 1A 07 01 00
    (b"\x52\x61\x72\x21\x1a\x07\x01\x00", ArchiveType.RAR5, "RAR5"),
    # RAR3: 52 61 72 21 1A 07 00 (RAR 4.x / RAR3-hp)
    (b"\x52\x61\x72\x21\x1a\x07\x00", ArchiveType.RAR3, "RAR3"),
    # ZIP: 50 4B 03 04 (标准本地文件头) / 50 4B 05 06 (空包/目录结束) / 50 4B 07 08 (分卷片段)
    (b"\x50\x4b\x03\x04", ArchiveType.ZIP, "ZIP"),
    (b"\x50\x4b\x05\x06", ArchiveType.ZIP, "ZIP (空目录)"),
    (b"\x50\x4b\x07\x08", ArchiveType.ZIP, "ZIP (分卷)"),
)

# 读取最大长度：覆盖最长魔数（RAR5 = 8字节）即可，少量冗余无影响
_READ_BYTES_MAX = 16


class ArchiveDetector:
    """
    压缩包类型检测器
    使用方法：
        det = ArchiveDetector()
        atype, label = det.detect("test.rar")
        if atype == ArchiveType.RAR5:
            ...
    """

    def detect(self, file_path: str) -> Tuple[ArchiveType, str]:
        """
        检测指定压缩包的真实类型
        :param file_path: 待检测文件路径
        :return: (ArchiveType 枚举, 可读类型名称字符串)；若文件不存在/不可读返回 UNKNOWN
        """
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return ArchiveType.UNKNOWN, "文件不存在"

        # 读取文件头部字节
        try:
            with open(path, "rb") as fp:
                header = fp.read(_READ_BYTES_MAX)
        except (PermissionError, OSError):
            return ArchiveType.UNKNOWN, "文件读取权限受限"

        # 匹配魔数（注意：按表顺序匹配，7Z与RAR5魔数更长，必须排在RAR3/ZIP前面）
        for magic, atype, label in _MAGIC_TABLE:
            if header.startswith(magic):
                # 若是 RAR 系列，补充子类型识别（按枚举值再细分）
                return atype, label
        return ArchiveType.UNKNOWN, "未识别的压缩包格式"

    def is_supported(self, file_path: str) -> bool:
        """
        简化版：判断文件是否为当前支持的四种压缩包之一
        :param file_path: 待检测文件路径
        :return: True 支持，False 不支持或读取失败
        """
        atype, _ = self.detect(file_path)
        return atype != ArchiveType.UNKNOWN

    def get_extension_candidates(self, atype: ArchiveType) -> Tuple[str, ...]:
        """
        根据检测出的真实类型，返回建议的扩展名列表（用于分卷处理或提示）
        """
        mapping = {
            ArchiveType.ZIP:      (".zip", ".zipx", ".z01"),
            ArchiveType.RAR3:     (".rar", ".r00", ".r01"),
            ArchiveType.RAR5:     (".rar", ".part1.rar"),
            ArchiveType.SEVEN_Z:  (".7z", ".7z.001"),
        }
        return mapping.get(atype, tuple())


if __name__ == "__main__":
    # 调试入口：传入一个或多个文件路径，即可查看检测结果
    import sys
    detector = ArchiveDetector()
    if len(sys.argv) < 2:
        print("用法: python archive_detector.py <file1> [file2 ...]")
        print("当前目录检测 .zip .rar .7z 示例如下，请自行传入参数测试。")
    else:
        for f in sys.argv[1:]:
            t, label = detector.detect(f)
            print(f"[{t.value:>7}] {label:<14} -> {f}")
