# ArchiveCracker 压缩包密码爆破工具

>  ArchiveCracker 是一个基于 Textual 的终端（TUI）压缩包密码爆破工具，内置 Hashcat 引擎，支持 GPU/CPU 加速，面向 ZIP / RAR / 7Z 等常见压缩包格式。

> 测试阶段,5060 8G 显卡, 14600KF, 开启GPU加速,3亿密码.所需时间约48秒

## 功能特性

- 密码破解：四种攻击模式
  - 字典攻击
  - 掩码攻击
  - 字典加规则
  - 暴力穷举
- 字典生成：三种生成方式
  - 经典字典生成
  - 社工字典生成
  - 掩码字典生成
- 工具自检：检查 Hashcat、John the Ripper 等依赖是否可用
- 设备信息：展示操作系统、CPU、内存、磁盘、GPU 等硬件信息
- 文件拖入：Windows Terminal 下可直接把文件拖入终端自动识别
- 实时进度：破解过程中显示状态、速度、百分比、候选密码、已破解密码
- 历史记录：每次破解结果保存在页面内，支持滚动查看
- 规则选择：自动读取规则目录，内置中文说明
- 掩码提示：掩码表达式输入时提供占位符说明与 Tab 快速补全

## 界面操作

主菜单：

```text
1. 密码破解
2. 字典生成
3. 工具自检
4. 帮助说明
5. 软件说明
6. 退出软件
```

## 快捷键

| 按键 | 功能 |
| --- | --- |
| W / S / 上下键 | 切换菜单项 |
| A / D | 返回上一层 / 进入确认 |
| J / K | 右侧内容下翻 / 上翻 |
| 空格 / 回车 | 进入 / 确认 / 执行 |
| ESC | 返回上一层 / 取消输入 / 中断破解 |
| Tab | 掩码表达式快速补全 |
| Ctrl+1 / 2 / 3 / 4 | 快速跳转破解 / 字典 / 自检 / 帮助 |
| Ctrl+Q | 退出软件 |

## 运行环境

- 操作系统：Windows（推荐）
- Python 3.13
- 依赖库：textual、rich、psutil
- 外部工具：Hashcat、John the Ripper（zip2john / rar2john / 7z2john）

## 安装

```powershell
git clone https://github.com/ycc77cn/ArchiveCracker
cd ArchiveCracker
pip install textual rich psutil
```

## 快速开始

```powershell
python main.py
```

程序启动后，使用 W/S 或上下键选择功能，回车进入。

## 密码破解

密码破解页面包含四种攻击模式。

### 1. 字典攻击

使用字典文件逐行尝试密码，速度最快。

配置项：

```text
0. 拖入文件（自动识别）
1. 压缩包路径（必填）
2. 字典文件路径（必填，多个用英文逗号分隔）
3. 工作负载（1-4）
4. 设备（auto / gpu / cpu）
5. 开始破解
```

工作负载说明：

```text
1 = 低（后台任务，不卡顿）
2 = 中低（轻度影响）
3 = 高（默认，显卡满载）
4 = 极致（系统可能卡顿）
```

### 2. 掩码攻击

按位置规则精准穷举。

掩码占位符：

| 占位符 | 含义 |
| --- | --- |
| ?d | 数字 0-9 |
| ?l | 小写字母 a-z |
| ?u | 大写字母 A-Z |
| ?s | 常见特殊字符 |
| ?a | 所有可打印字符 |
| ?1-?4 | 自定义字符集 |

示例：

```text
?d?d?d?d = 0000-9999
pass?d?d?d = pass000-pass999
```

掩码表达式输入时，可以直接输入 `?`，再输入 d/l/u/s/a/1-4；也可以按 Tab 自动补全 `?d`。

### 3. 字典加规则

对字典中的每个单词进行规则变形，扩大候选集。

配置项：

```text
0. 拖入文件（自动识别）
1. 压缩包路径（必填）
2. 字典文件路径（必填）
3. 选择规则文件（自动读取 + 中文说明）
4. 规则文件路径（手动填写）
5. 工作负载（1-4）
6. 设备（auto / gpu / cpu）
7. 开始破解
```

规则选择弹窗支持 W/S 选择、D 或回车确认、A 或 ESC 返回。每个规则都有中文说明。

### 4. 暴力穷举

按字符集与长度范围全空间穷举。

配置项：

```text
0. 拖入文件（自动识别）
1. 压缩包路径（必填）
2. 小写字母
3. 大写字母
4. 数字
5. 特殊字符
6. 自定义字符集（可选）
7. 最小长度
8. 最大长度
9. 快速模板
10. 工作负载（1-4）
11. 设备（auto / gpu / cpu）
12. 开始破解
```

暴力穷举使用 Hashcat 增量模式，从最小长度到最大长度自动递增。

## 字典生成

### 经典字典

基于字符集笛卡尔积生成密码。

### 社工字典

根据个人信息（姓名、生日、手机号、QQ、微信号等）组合生成。

### 掩码字典

按掩码占位符模式生成。

## 工具自检

检查 Hashcat、John the Ripper 等外部工具是否可用，并显示检测结果。

## 设备信息

首页右侧显示设备信息：

```text
OS
Hostname
CPU
内存
磁盘
GPU
软件信息
```

软件信息包含：

```text
开发者: 杨CC
开源地址: https://github.com/ycc77cn/ArchiveCracker
B站: 疯狂的杨CC
粉丝群: 660264846
```

## 拖入文件

在 Windows Terminal 中，可以直接把文件拖入终端窗口，程序按扩展名自动识别：

```text
zip / rar / 7z -> 压缩包路径
txt / dic / lst -> 字典文件
rule -> 规则文件
```

## 打包 exe

使用 PyInstaller 打包 onedir 模式：

```powershell
python -m PyInstaller --noconfirm --onedir --name ArchiveCracker --icon logo.png --add-data "bin;bin" --collect-all textual --collect-all rich --hidden-import psutil --distpath dist --workpath build main.py
```

输出位置：

```text
dist\ArchiveCracker\ArchiveCracker.exe
dist\ArchiveCracker\_internal\bin
```

注意：不要加 `--windowed`，否则 TUI 控制台会隐藏。

## 项目结构

```text
ArchiveCracker/
├─ main.py
├─ logo.png
├─ README.md
├─ .gitignore
├─ core/
│  ├─ __init__.py
│  ├─ path_manager.py
│  ├─ archive_detector.py
│  ├─ hash_extractor.py
│  ├─ cracker.py
│  ├─ dict_generator.py
│  └─ hardware_info.py
├─ bin/
│  ├─ windows/
│  │  ├─ hashcat/
│  │  └─ john/
│  └─ linux/（可选）
└─ data/
   ├─ dictionaries/
   └─ output/
```

## 常见问题

### 1. 提示 Hashcat 不可用

检查 `bin/windows/hashcat/` 是否存在 hashcat 可执行文件。

### 2. 上下键在 PowerShell 中不可用

部分 PowerShell 环境下方向键无法可靠送达，请使用 W/S 切换菜单、A/D 返回/确认、J/K 滚动内容。

### 3. 破解历史太多看不到

使用 J/K 或 PageUp/PageDown 滚动右侧内容。

### 4. 破解结果保存在哪里

出于安全考虑，破解结果不保存到文件，密码只在页面历史中显示。

### 5. 能否在 Linux 运行

当前项目自带 Windows 工具链。Linux 需要准备 `bin/linux` 下的 Hashcat 和 John 工具后运行。

## 开发者

- 开发者：杨CC
- 开源地址：https://github.com/ycc77cn/ArchiveCracker
- B站：疯狂的杨CC
- 粉丝群：660264846

## 开源协议

项目开源，具体开源协议以仓库内 LICENSE 文件为准。
