"""imports/ —— 把老客户端 / 老网页导出的数据包搬进新后端。

分四层，每层只干一件事，读代码时不用同时想两件事：

    package.py    字节 → 原始字典（认 ZIP / JSON，校验版本）
    normalize.py  原始字典 → 归一化结构（翻译老字段名、解出图片字节）
    apply.py      归一化结构 → 数据库（建会话、落消息、存图）
    service.py    把上面三步 + 幂等 + 撤销串成"一次导入"

`app/api/imports.py` 只负责收文件、调 service、把结果转成对外形状。
"""

from app.imports.package import PackageError

__all__ = ["PackageError"]
