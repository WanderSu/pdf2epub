# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec:onedir 模式。

产物: dist_cli/cli/cli.exe + dist_cli/cli/_internal/…

为什么从 onefile 改 onedir(布局调整,见 scripts/build_release.py):
- onefile 每次启动都要把 ~95MB 解压到 %TEMP%\\_MEIxxxx(冷启 1~3 秒),
  桌面端每次转换 spawn 一次引擎,用户每转一本书都要付一次这个税;
- 杀毒软件对「自解压 exe」的启发式误报显著高于普通目录形态;
- 单文件没法差量更新,升级永远全量传 95MB。

发布布局(绿色版 zip,引擎目录与壳、config 同级):
    pdf2epub.exe          桌面壳
    cli/cli.exe           引擎入口(本 spec 产物)
    cli/_internal/…       Python 运行时 + 依赖
    config/…              运行时配置(在安装根,不在 cli/ 里)
"""
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('pymupdf')

#: 明确不需要的收集物(按目标名前缀过滤):
#: - pymupdf/mupdf-devel: MuPDF 的 C 头文件/开发库,只有二次开发 native 扩展才要
#: - __pycache__: 字节码缓存,运行时按需重生成,打包进去纯占体积
_DROP_PREFIXES = ('pymupdf/mupdf-devel', '__pycache__')


def _keep(target_name: str) -> bool:
    normalized = target_name.replace('\\', '/')
    return not any(normalized.startswith(p) for p in _DROP_PREFIXES)


a = Analysis(
    ['src/cli.py'],
    pathex=['src'],
    binaries=[],
    datas=[d for d in datas if _keep(d[0])],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# collect_data_files 与 hook 收集的 binaries 里同样可能混进开发文件/缓存
a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='cli',
)
