# pdf2epub 引擎包
#
# 为什么要有这一层(而不是把模块平铺在 site-packages 根):``markdown`` / ``epub`` /
# ``clean`` / ``events`` / ``paths`` 这些名字太通用,平铺安装会与 PyPI 的
# ``markdown`` 等同名包互相覆盖(pip 安装时静默合并目录、卸载时互相删文件)。
# 引擎自身是应用不是库,但 ``pip install`` / ``uv sync`` 会把包装进环境,所以必须
# 有自己的命名空间。
