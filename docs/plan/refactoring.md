主要改善点

  1. bot/ 承担太多核心职责
     api/server.py:10 直接 import bot.database、bot.merge、bot.trace，说明这些并不只是 Discord bot 代码，而是全局业务能力。建议中期改成：

     life_tracker/
       core/          # timeline merge、业务规则、领域模型
       storage/       # database.py、schema/migrations
       ai/            # provider、prompts、tools、trace
       integrations/
         discord/     # discord_bot.py
       api/           # FastAPI routers
       scheduler/     # scheduler.py
     短期不用大搬家，但新代码建议别继续塞进 bot/。
     短期不用大搬家，但新代码建议别继续塞进 bot/。

  2. api/server.py 已经偏大
     它现在 623 行，包含 timeline、events、projects、traces、admin preset、健康检查等多个领域。建议拆成：

     api/
       app.py
       deps.py
       routers/
         timeline.py
         projects.py
         admin.py
         traces.py
         health.py

     另外现在用全局 db 注入：api/server.py:24，小项目可用，但更稳的是 FastAPI dependency，比如 get_db()。

  3. API 层有少量存储细节泄漏
     比如 reminders 端点直接 db._get_conn() 查 SQL：api/server.py:119。这类逻辑最好收到 Database 方法里，否则以后 schema 改动会同时影响 API 层。

  4. config.py import-time 副作用较重
     main.py:13 需要在 import config 前设置环境变量，说明配置加载和进程启动耦合比较紧。更科学的方向是把配置做成显式函数：

     settings = load_settings(api_only=args.api_only)

     避免 import 时 sys.exit()、读写状态、校验混在一起。

  5. README 和实际文件有轻微不同步
     README 文档索引里提到 docs/dispatch-escalation-samples.md：README.md:143，但当前 docs/ 下没看到这个文件。结构说明本身很有价值，建议保持它和实际目录同步。

  6. 前端结构可以再按功能分组
     现在 frontend/src/app/components/ 下页面级组件和普通组件混在一起。建议逐步改成：

     frontend/src/
       app/
       features/
         dashboard/
         rhythm/
         traces/
         admin/
         projects/
       components/ui/
       styles/

     components/ui/ 保持现状没问题，主要是业务组件可以按 feature 收拢。

  7. 运行产物已经被 ignore，但本地视图较吵
     .gitignore 对 data/、data-dev/、frontend/dist/、frontend/node_modules/、__pycache__/ 都有处理：.gitignore:46。这点是对的。只是日常看结构时要过滤这些目录，否
     则会误判仓库复杂度。

  优先级建议

  短期最值得做：更新 README、把 api/server.py 拆 router、把直接 SQL 收回 Database。

  中期再做：把 bot/ 中非 Discord 专属的模块迁到 core/storage/ai 这类中性包名。
