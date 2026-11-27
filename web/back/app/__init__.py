"""BakerAI 新后端。

目录分工（照着找代码就行）：
  app/settings.py   配置
  app/db.py         数据库连接
  app/models/       13 张表的 ORM 模型（镜像 schema/*.sql）
  app/repos/        所有 SQL 只在这里
  app/api/          路由：解析请求 → 调 repo → 组装响应
  app/errors.py     唯一错误出口
  app/migrate.py    执行 schema/*.sql
"""
