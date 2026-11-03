#!/usr/bin/env python3
"""Build a local, privacy-preserving weekly Lyph technology-stack draft.

This tool never uploads project files or agent transcripts. It extracts only
known technology names and aggregate counts. Publishing is a separate owner
review step.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+
    tomllib = None


CATALOG = [
    ("TypeScript", "🟦", "JavaScript 的静态类型扩展，用于在开发阶段发现接口和数据结构问题。", "为 API 响应、组件属性和业务对象定义类型；用 tsc 检查，再由构建工具编译。", ["typescript"], ["typescript"]),
    ("JavaScript", "🟨", "浏览器与 Node.js 常用脚本语言，用于页面交互、工具脚本和服务端应用。", "用 ES modules 组织功能，优先拆分纯函数，再通过浏览器或 Node.js 运行。", ["javascript", "node.js"], ["javascript"]),
    ("React", "⚛️", "组件化 UI 库，用状态和属性组合可复用的交互界面。", "把页面拆为小组件，用 props 传数据、用 state 管理交互，并保持渲染逻辑可预测。", ["react"], ["react"]),
    ("Next.js", "▲", "基于 React 的 Web 框架，提供路由、服务端渲染和生产构建。", "按路由组织页面；将需要密钥的请求放在服务端，将交互状态留在客户端组件。", ["next.js", "nextjs"], ["next"]),
    ("Vue", "🟩", "渐进式前端框架，适合构建组件化交互页面。", "使用单文件组件、响应式状态和 props/events 拆分页面功能。", ["vue.js", "vue"], ["vue"]),
    ("Vite", "⚡", "前端开发服务器与打包工具，提供快速热更新和生产构建。", "用插件接入框架，开发时运行 dev server，发布前执行生产 build。", ["vite"], ["vite"]),
    ("Tailwind CSS", "🎨", "实用优先的 CSS 工具集，通过组合样式类构建页面布局。", "把间距、布局、响应式和状态样式放在组件 class 中，重复模式再抽成组件。", ["tailwind css", "tailwindcss"], ["tailwindcss"]),
    ("shadcn/ui", "🧩", "基于 Radix 与 Tailwind 的可定制 React 组件方案。", "选择需要的组件源码放入项目，按产品设计修改，不把组件库当成不可改的黑盒。", ["shadcn/ui", "shadcn ui"], ["shadcn"]),
    ("Framer Motion", "🎞️", "React 动画库，用于声明式过渡、布局动画和手势交互。", "为状态切换配置短时长过渡，减少大面积模糊和影响滚动的动画。", ["framer motion", "motion/react"], ["framer-motion", "motion"]),
    ("Java", "☕", "面向对象语言，常用于后端服务、企业应用和数据处理工具。", "用清晰的领域对象组织业务逻辑，依赖构建工具编译，并以单元测试固定行为。", ["java"], ["java"]),
    ("Spring Boot", "🍃", "Java 服务端框架，简化 Web API、配置和依赖管理。", "按 Controller、Service、Repository 分层；用配置文件管理环境差异和健康检查。", ["spring boot", "springboot"], ["spring-boot-starter", "spring-boot"]),
    ("Maven", "📦", "Java 项目构建和依赖管理工具。", "在 pom.xml 锁定依赖，通过 Maven 生命周期编译、测试和打包。", ["maven"], ["maven"]),
    ("Gradle", "🛠️", "基于任务图的构建工具，常用于 Java、Kotlin 和 Android。", "用 build.gradle 声明插件、依赖与任务，使用 wrapper 固定项目构建版本。", ["gradle"], ["gradle"]),
    ("Python", "🐍", "通用编程语言，常用于 API、自动化、数据分析与机器学习。", "按功能拆模块，用虚拟环境隔离依赖，并通过类型提示和自动化检查维护代码。", ["python"], ["python"]),
    ("FastAPI", "🚀", "基于 Python 类型标注构建异步 HTTP API 的框架。", "使用 Pydantic 模型校验输入，路由保持薄层，外部 I/O 设置超时并明确错误。", ["fastapi"], ["fastapi"]),
    ("Flask", "🌶️", "轻量 Python Web 框架，适合小型服务和可组合的 HTTP 应用。", "以蓝图划分路由，将业务逻辑抽到服务层，并在应用工厂中管理配置。", ["flask"], ["flask"]),
    ("Django", "🎸", "包含 ORM、路由和管理界面的 Python Web 框架。", "用模型表达数据关系，表单和视图校验输入，并通过迁移管理数据库结构。", ["django"], ["django"]),
    ("Pydantic", "✅", "Python 数据校验和序列化库，常用于 API 输入输出模型。", "为外部输入定义模型和约束，避免业务函数重复解析不可信字典。", ["pydantic"], ["pydantic"]),
    ("SQLAlchemy", "🗃️", "Python SQL 工具包和 ORM，支持声明式模型及显式 SQL。", "为事务边界和连接生命周期建立上下文，复杂查询保留可审查的 SQL 语义。", ["sqlalchemy"], ["sqlalchemy"]),
    ("LangChain", "🔗", "用于组合模型、提示、检索器和工具的应用框架。", "将检索、工具调用和答案生成拆成可观测步骤，并为权限与错误设边界。", ["langchain"], ["langchain"]),
    ("MCP", "🔌", "连接 AI 客户端与外部工具、资源的开放协议。", "公开最小只读工具集合，校验每个参数与调用身份，不提供任意 SQL 或 shell。", ["model context protocol", "mcp"], ["mcp"]),
    ("OpenAI 兼容 API", "🤖", "遵循 OpenAI 风格请求格式的模型 API 接口。", "将端点、模型和密钥置于服务端配置，客户端只发送业务输入，不暴露凭证。", ["openai compatible", "openai api"], ["openai"]),
    ("SQLite", "🪶", "嵌入式关系数据库，适合单机应用和低运维数据存储。", "用事务保护多步写入，启用 WAL 时按 SQLite 备份规则处理数据库与日志。", ["sqlite"], ["sqlite"]),
    ("sqlite-vec", "🧭", "SQLite 向量搜索扩展，可将相似度检索放在本地数据库中。", "为向量表保存模型版本与维度，检索后仍按权限、版本和条件校验结果。", ["sqlite-vec", "sqlite vec"], ["sqlite-vec"]),
    ("MySQL", "🐬", "常见关系数据库，适用于多用户业务数据和事务型查询。", "为过滤与关联列建索引，使用参数化 SQL 和明确的事务隔离边界。", ["mysql"], ["mysql-connector", "pymysql", "mysqlclient"]),
    ("PostgreSQL", "🐘", "功能丰富的关系数据库，支持事务、索引和 JSON 数据。", "以迁移管理 schema，使用参数化查询并按实际查询计划优化索引。", ["postgresql", "postgres"], ["psycopg", "asyncpg"]),
    ("Redis", "🔴", "内存数据结构服务，常用于缓存、限流和短期协调。", "为 key 设计过期策略和命名空间；缓存失效时要保证核心请求仍能降级。", ["redis"], ["redis", "ioredis"]),
    ("Elasticsearch", "🔎", "分布式全文检索与聚合引擎。", "明确 mapping、分词和索引版本；索引切换使用别名并保留回滚路径。", ["elasticsearch"], ["elasticsearch", "elasticsearch-py"]),
    ("Neo4j", "🕸️", "属性图数据库，适合表达实体及多跳关系。", "先定义节点与关系类型，再以参数化 Cypher 限制查询深度和结果规模。", ["neo4j"], ["neo4j"]),
    ("PyTorch", "🔥", "Python 深度学习框架，支持张量计算和自动微分。", "固定随机种子与数据划分，分别记录训练配置、权重和评估指标。", ["pytorch", "torch"], ["torch"]),
    ("NumPy", "🔢", "Python 数值计算基础库，提供多维数组和向量化运算。", "用数组运算替代逐项 Python 循环，明确 dtype、维度和缺失值处理。", ["numpy"], ["numpy"]),
    ("pandas", "🐼", "Python 表格数据处理库，用于清洗、连接和汇总数据。", "先检查列类型与缺失值，再进行 groupby/merge，并在导出前验证行数与口径。", ["pandas"], ["pandas"]),
    ("scikit-learn", "🧪", "Python 机器学习工具包，提供预处理、传统模型和评估方法。", "用 Pipeline 固定预处理与模型步骤，按正确的数据划分评估并记录随机种子。", ["scikit-learn", "scikit learn"], ["scikit-learn"]),
    ("Jupyter", "📓", "交互式笔记本环境，适合探索、可视化和记录分析过程。", "将探索笔记转成可重复执行的步骤，保留数据来源、环境和结果解释。", ["jupyter"], ["jupyter", "ipykernel"]),
    ("pyecharts", "📊", "Python 图表库，可生成交互式 ECharts 可视化。", "统一数据口径和图表选项，输出前检查空值、单位、图例与移动端尺寸。", ["pyecharts"], ["pyecharts"]),
    ("Rust", "🦀", "强调内存安全和性能的系统编程语言。", "用类型表达状态与错误，通过 Cargo 管理依赖，并优先采用安全 Rust。", ["rust"], ["rust"]),
    ("Tauri", "🪟", "使用系统 WebView 与 Rust 后端构建轻量桌面应用的框架。", "只向前端开放必要 commands 和 capabilities，限制文件与进程权限。", ["tauri"], ["tauri"]),
    ("C++", "⚙️", "通用系统与高性能应用语言，支持面向对象和泛型编程。", "明确所有权与资源生命周期，使用构建系统并以 sanitizers/测试检查边界。", ["c++", "cpp"], ["c++"]),
    ("C", "🔧", "底层过程式语言，常用于系统、嵌入式和性能敏感组件。", "显式检查缓冲区长度和返回码，集中管理资源释放并运行静态分析。", [" c ", "语言 c"], ["c"]),
    ("Go", "🐹", "带有内置并发和部署工具链的编译型语言。", "用 context 传递取消与超时，接口保持小型，运行 go test 和 go vet。", ["golang", "go language"], ["go"]),
    ("Docker", "🐳", "容器镜像与运行环境工具，用于封装和交付服务。", "使用多阶段构建、非 root 用户和固定镜像版本，容器只挂载必要目录。", ["docker"], ["docker"]),
    ("Docker Compose", "📦", "以 YAML 描述多容器应用及其网络、卷和健康检查。", "将服务依赖和健康检查写进 compose，部署前验证配置并使用受控卷。", ["docker compose", "compose.yaml"], ["docker compose"]),
    ("Nginx", "🌐", "反向代理和 Web 服务器，可处理 TLS、路由和静态资源。", "按路径设置代理和安全头，部署前运行 nginx -t，变更后核对外部健康检查。", ["nginx"], ["nginx"]),
    ("MinIO", "🪣", "兼容 S3 API 的对象存储服务，可管理文件与备份。", "将对象 bucket 设为私有，服务端使用最小权限凭证，并验证恢复链路。", ["minio"], ["minio"]),
    ("Git", "🌱", "分布式版本控制工具，用于追踪源码变化和回滚。", "按可审查的主题提交变更，提交前检查 diff、忽略敏感文件并记录回滚点。", ["git"], ["git"]),
    ("Playwright", "🎭", "浏览器自动化与端到端测试框架。", "用稳定角色或标签定位元素，等待可观察状态，并隔离真实用户数据。", ["playwright"], ["playwright"]),
    ("pytest", "🧫", "Python 测试框架，支持 fixture 和参数化测试。", "测试公开输入输出和失败路径，fixture 管理隔离资源并保证 teardown。", ["pytest"], ["pytest"]),
    ("k6", "📈", "用 JavaScript 编写负载场景的性能测试工具。", "先定义用户场景和阈值，再逐级加压并把失败数、延迟和资源指标一起记录。", ["k6"], ["k6"]),
    ("GitHub Actions", "🐙", "GitHub 托管的 CI/CD 工作流服务。", "为构建和检查设最小权限，固定 action 版本，secret 只通过受保护变量注入。", ["github actions"], ["github-actions"]),
    ("APScheduler", "⏰", "Python 进程内定时任务调度库。", "为任务设时区、幂等键和失败状态；多副本场景需避免重复调度。", ["apscheduler"], ["apscheduler"]),
    ("LangChain4j", "🦜", "Java 生态的 LLM、工具和检索应用开发库。", "通过接口封装模型和检索器，工具参数设白名单并记录来源版本。", ["langchain4j"], ["langchain4j"]),
    ("OpenCV", "👁️", "跨平台图像处理与计算机视觉库。", "统一颜色空间与尺寸，对文件输入做容量限制，并用小样本确认处理结果。", ["opencv", "cv2"], ["opencv-python", "opencv"]),
    ("WeChat Mini Program", "💬", "微信小程序平台，用页面、组件和平台 API 构建轻量应用。", "遵循小程序页面生命周期，敏感能力先校验用户授权，并验证真机布局。", ["wechat mini program", "微信小程序", "miniprogram"], ["miniprogram"]),
    ("Codex", "🧠", "OpenAI 的编码代理，可在本地会话中协助分析、修改和验证项目。", "按项目说明给出明确任务，审阅文件改动与命令结果，再决定是否合并或发布。", ["codex", "codex cli", "openai codex"], []),
    ("OpenCode", "🧰", "可连接多种模型的开源编码代理，保存本地项目会话。", "选择模型和项目目录后，通过会话指令完成代码探索；发布前审阅变更与权限。", ["opencode"], []),
    ("WorkBuddy", "🐧", "腾讯的桌面 AI Agent，可围绕本地工作区完成多步骤任务。", "把任务限定到目标工作区，检查它拟读写的文件，并在应用历史中复核交付。", ["workbuddy"], []),
]

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".next", ".turbo", ".venv", "venv",
    "env", "dist", "build", "target", "vendor", "coverage", "__pycache__",
    ".pytest_cache", "site-packages", "packages", "bower_components", ".idea",
}
MANIFESTS = {"package.json", "pyproject.toml", "cargo.toml", "pom.xml", "go.mod", "dockerfile"}
SECRET_PATTERN = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|password|passwd|secret|authorization|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9]{20,})"
)
MAX_MANIFEST_BYTES = 2_000_000
MAX_SESSION_BYTES = 40_000_000


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def package_name(value: str) -> str:
    value = value.strip().lower()
    value = re.split(r"[<=>!~;\[]", value, 1)[0]
    return value.rsplit("/", 1)[-1]


def catalog_indexes():
    by_package: dict[str, int] = {}
    patterns: list[tuple[re.Pattern[str], int]] = []
    for index, (name, _icon, _description, _how, aliases, packages) in enumerate(CATALOG):
        for package in packages:
            by_package[package_name(package)] = index
        for alias in aliases:
            escaped = re.escape(alias.strip())
            if alias.startswith(" ") or alias.endswith(" "):
                patterns.append((re.compile(escaped, re.IGNORECASE), index))
            elif re.fullmatch(r"[A-Za-z0-9.+#/_ -]+", alias):
                patterns.append((re.compile(r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])", re.IGNORECASE), index))
    return by_package, patterns


BY_PACKAGE, TERM_PATTERNS = catalog_indexes()


def add_package_names(value, found: set[int]) -> None:
    if isinstance(value, str):
        name = package_name(value)
        if name in BY_PACKAGE:
            found.add(BY_PACKAGE[name])
    elif isinstance(value, dict):
        for name in value:
            add_package_names(name, found)
    elif isinstance(value, list):
        for item in value:
            add_package_names(item, found)


def read_json(path: Path):
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        return None
    return json.loads(path.read_text("utf-8-sig"))


def project_evidence(project: Path) -> set[int]:
    found: set[int] = set()
    for manifest in project.iterdir():
        if not manifest.is_file() or manifest.stat().st_size > MAX_MANIFEST_BYTES:
            continue
        lower = manifest.name.lower()
        try:
            if lower == "package.json":
                data = read_json(manifest)
                if isinstance(data, dict):
                    for group in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                        add_package_names(data.get(group, {}), found)
                    if any(key in data for key in ("scripts", "dependencies", "devDependencies")):
                        found.add(next(i for i, row in enumerate(CATALOG) if row[0] == "JavaScript"))
            elif lower == "pyproject.toml" and tomllib:
                data = tomllib.loads(manifest.read_text("utf-8"))
                project_data = data.get("project", {})
                add_package_names(project_data.get("dependencies", []), found)
                optional = project_data.get("optional-dependencies", {})
                add_package_names(optional, found)
                tool = data.get("tool", {})
                for manager in ("poetry", "uv", "pdm", "hatch"):
                    add_package_names(tool.get(manager, {}), found)
                if "project" in data:
                    found.add(next(i for i, row in enumerate(CATALOG) if row[0] == "Python"))
            elif lower.startswith("requirements") and lower.endswith(".txt"):
                for line in manifest.read_text("utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line and not line.startswith(("#", "-", "http://", "https://")):
                        add_package_names(line, found)
            elif lower == "cargo.toml" and tomllib:
                data = tomllib.loads(manifest.read_text("utf-8"))
                for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                    add_package_names(data.get(key, {}), found)
                found.add(next(i for i, row in enumerate(CATALOG) if row[0] == "Rust"))
            elif lower == "pom.xml":
                root = ET.fromstring(manifest.read_text("utf-8"))
                for node in root.iter():
                    if node.tag.endswith("artifactId") and node.text:
                        add_package_names(node.text, found)
                found.add(next(i for i, row in enumerate(CATALOG) if row[0] == "Java"))
            elif lower == "go.mod":
                found.add(next(i for i, row in enumerate(CATALOG) if row[0] == "Go"))
                text = manifest.read_text("utf-8", errors="ignore")
                if "github.com/gin-gonic/gin" in text:
                    pass
            elif lower == "dockerfile":
                found.add(next(i for i, row in enumerate(CATALOG) if row[0] == "Docker"))
        except (OSError, ValueError, TypeError, StopIteration, ET.ParseError):
            continue

    # Source file extensions are independent evidence for language use.
    suffix_counts: dict[str, int] = {}
    try:
        for path in project.iterdir():
            if path.is_file() and path.stat().st_size <= MAX_MANIFEST_BYTES:
                suffix_counts[path.suffix.lower()] = suffix_counts.get(path.suffix.lower(), 0) + 1
            elif path.is_dir() and path.name in {"src", "app", "lib", "tests", "test"}:
                for base, dirs, files in os.walk(path):
                    dirs[:] = [d for d in dirs if d.lower() not in IGNORED_DIRS]
                    for name in files[:2000]:
                        suffix = Path(name).suffix.lower()
                        suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
    except OSError:
        pass
    extension_tech = {".py": "Python", ".java": "Java", ".rs": "Rust", ".go": "Go", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript", ".cpp": "C++", ".cc": "C++", ".c": "C", ".ipynb": "Jupyter"}
    for ext, name in extension_tech.items():
        if suffix_counts.get(ext, 0):
            found.add(next(i for i, row in enumerate(CATALOG) if row[0] == name))
    return found


def discover_projects(roots: list[Path], max_depth: int = 7) -> dict[Path, set[int]]:
    projects: dict[Path, set[int]] = {}
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        root = root.resolve()
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            try:
                depth = len(current_path.relative_to(root).parts)
            except ValueError:
                dirs[:] = []
                continue
            dirs[:] = [name for name in dirs if name.lower() not in IGNORED_DIRS and not name.startswith(".")]
            if depth >= max_depth:
                dirs[:] = []
            lower_files = {name.lower() for name in files}
            if lower_files.intersection(MANIFESTS) or any(name.startswith("requirements") and name.endswith(".txt") for name in lower_files):
                evidence = project_evidence(current_path)
                if evidence:
                    projects[current_path] = evidence
    return projects


def text_fragments(value, key: str = ""):
    if isinstance(value, str):
        if key.lower() in {"text", "message", "content", "input", "output"}:
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from text_fragments(item, key)
    elif isinstance(value, dict):
        for child_key, child in value.items():
            yield from text_fragments(child, str(child_key))


def technology_mentions(text: str) -> set[int]:
    if not text or SECRET_PATTERN.search(text):
        return set()
    return {index for pattern, index in TERM_PATTERNS if pattern.search(text)}


def codex_mentions(codex_home: Path, cutoff: dt.datetime) -> tuple[dict[int, int], int, int]:
    mentions: dict[int, int] = {}
    files_seen = files_skipped = 0
    roots = [codex_home / "sessions", codex_home / "archived_sessions"]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
                if modified < cutoff or path.stat().st_size > MAX_SESSION_BYTES:
                    continue
                files_seen += 1
                found: set[int] = set()
                with path.open("r", encoding="utf-8", errors="ignore") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        payload = event.get("payload", {}) if isinstance(event, dict) else {}
                        role = payload.get("role") if isinstance(payload, dict) else None
                        kind = payload.get("type") if isinstance(payload, dict) else None
                        if role not in {"user", "assistant"} and kind not in {"user_message", "agent_message"}:
                            continue
                        for fragment in text_fragments(payload):
                            found.update(technology_mentions(fragment))
                for index in found:
                    mentions[index] = mentions.get(index, 0) + 1
            except (OSError, PermissionError):
                files_skipped += 1
    return mentions, files_seen, files_skipped


def opencode_mentions(database: Path, cutoff: dt.datetime) -> tuple[dict[int, int], int, str]:
    mentions: dict[int, int] = {}
    if not database.exists():
        return mentions, 0, "not_found"
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"message", "part"}.issubset(tables):
            connection.close()
            return mentions, 0, "schema_unsupported"
        connection.execute("PRAGMA query_only=ON")
        cutoff_ms = int(cutoff.timestamp() * 1000)
        messages = connection.execute(
            "SELECT m.id, m.data AS message_data, p.data AS part_data "
            "FROM message AS m JOIN part AS p ON p.message_id=m.id "
            "WHERE m.time_created >= ? AND p.time_created >= ? ORDER BY m.id",
            (cutoff_ms, cutoff_ms),
        )
        scanned = 0
        current_id = None
        current_role = None
        current_found: set[int] = set()

        def finish_message():
            nonlocal scanned, current_found
            if current_role in {"user", "assistant"}:
                for index in current_found:
                    mentions[index] = mentions.get(index, 0) + 1
                scanned += 1
            current_found = set()

        for message in messages:
            try:
                if message["id"] != current_id:
                    if current_id is not None:
                        finish_message()
                    current_id = message["id"]
                    current_role = None
                    envelope = json.loads(message["message_data"])
                    current_role = envelope.get("role")
                if current_role not in {"user", "assistant"}:
                    continue
                data = json.loads(message["part_data"])
                if data.get("type") == "text" and isinstance(data.get("text"), str):
                    current_found.update(technology_mentions(data["text"]))
            except (ValueError, TypeError, KeyError):
                continue
        if current_id is not None:
            finish_message()
        connection.close()
        return mentions, scanned, "ok"
    except (sqlite3.Error, OSError, PermissionError):
        return mentions, 0, "readonly_unavailable"


def workbuddy_mentions(export_dir: Path, cutoff: dt.datetime) -> tuple[dict[int, int], int, int]:
    mentions: dict[int, int] = {}
    files_seen = files_skipped = 0
    if not export_dir.exists():
        return mentions, files_seen, files_skipped
    extensions = {".md", ".txt", ".json", ".jsonl"}
    for path in export_dir.rglob("*"):
        try:
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
            if modified < cutoff or path.stat().st_size > MAX_SESSION_BYTES:
                continue
            found: set[int] = set()
            content = path.read_text("utf-8", errors="ignore")
            if path.suffix.lower() in {".json", ".jsonl"}:
                try:
                    value = json.loads(content) if path.suffix.lower() == ".json" else [json.loads(line) for line in content.splitlines() if line.strip()]
                    fragments = text_fragments(value)
                except ValueError:
                    fragments = [content]
            else:
                fragments = [content]
            for fragment in fragments:
                found.update(technology_mentions(fragment))
            for index in found:
                mentions[index] = mentions.get(index, 0) + 1
            files_seen += 1
        except (OSError, PermissionError):
            files_skipped += 1
    return mentions, files_seen, files_skipped


def fetch_published_names(url: str) -> tuple[set[str], str]:
    if not url:
        return set(), "disabled"
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Lyph-Tech-Stack-Review/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
        site = payload.get("site", payload)
        stack = site.get("stack", []) if isinstance(site, dict) else []
        names = {norm(str(item.get("name") or item.get("title") or "")) for item in stack if isinstance(item, dict)}
        names.discard("")
        return names, "ok"
    except Exception:
        return set(), "unavailable"


def proficiency(project_count: int, session_sources: int) -> int:
    # Evidence estimate for review only; conversation mentions have a small cap.
    return min(95, 22 + min(project_count, 5) * 13 + min(session_sources, 3) * 2)


def render_html(report: dict) -> str:
    cards = []
    for item in report["technologies"]:
        bars = max(0, min(100, int(item["suggested_progress"])))
        cards.append(
            '<article class="card"><div class="head"><span class="icon">{icon}</span><div><h2>{name}</h2>'
            '<small>{evidence}</small></div></div><p>{description}</p><p class="how"><b>如何使用：</b>{how}</p>'
            '<div class="meter"><span style="width:{progress}%"></span></div><div class="foot">实作证据估算 · {progress}% · {publication} · 待人工复核</div></article>'.format(
                icon=html.escape(item["icon"]), name=html.escape(item["name"]),
                evidence=html.escape(item["evidence_summary"]), description=html.escape(item["description"]),
                how=html.escape(item["how"]), progress=bars,
                publication=("公开栈已有" if item["already_published"] is True else "建议新增候选" if item["already_published"] is False else "公网基线未读取"),
            )
        )
    if not cards:
        cards.append('<p class="empty">本周没有发现可供复核的新技术条目。采集摘要仍可在 JSON 报告中查看。</p>')
    generated = html.escape(report["generated_at"])
    summary = report["summary"]
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lyph 技术栈周更草案</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ee;color:#20211f;font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1040px;margin:0 auto;padding:40px 22px 64px}h1{font-size:32px;letter-spacing:-.03em;margin:8px 0}.eyebrow,small{color:#6f706d}.notice{border:1px solid #d7d3ca;background:#fffdf8;border-radius:14px;padding:16px 18px;margin:20px 0 26px}.notice strong{color:#954b2e}.summary{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 28px}.pill{background:#e9e6df;border-radius:999px;padding:5px 12px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px}.card{background:#fff;border:1px solid #e4e1da;border-radius:16px;padding:18px;box-shadow:0 5px 22px #1e1c1609}.head{display:flex;align-items:center;gap:12px}.icon{font-size:27px}.card h2{font-size:19px;margin:0}.card p{margin:12px 0;color:#434541}.how{font-size:14px}.meter{height:8px;border-radius:8px;background:#e9e7e2;overflow:hidden;margin-top:17px}.meter span{display:block;height:100%;background:#628d76;border-radius:inherit}.foot{font-size:12px;color:#777;margin-top:5px}.empty{background:white;padding:20px;border-radius:12px}.meta{font-size:13px;color:#676863}code{background:#ebe9e4;padding:2px 5px;border-radius:4px}@media(max-width:600px){main{padding:26px 14px}h1{font-size:27px}}
</style><main><span class="eyebrow">LOCAL REVIEW · 每周自动采集草案</span><h1>Lyph 技术栈周更</h1><div class="meta">生成时间：""" + generated + """</div>
<div class="notice"><strong>待人工复核，未发布。</strong> 本报告只展示技术名称、图标、用途、使用方式和基于实作证据的进度估算；不会包含项目路径、会话标题或对话原文，也不会自动改写公网资料。</div>
<div class="summary"><span class="pill">项目：""" + str(summary["projects_scanned"]) + """ 个</span><span class="pill">公开栈匹配：""" + html.escape(summary["public_baseline_status"]) + """</span><span class="pill">Codex 会话：""" + str(summary["codex_files_scanned"]) + """</span><span class="pill">OpenCode 消息：""" + str(summary["opencode_messages_scanned"]) + """</span><span class="pill">WorkBuddy 导出：""" + str(summary["workbuddy_files_scanned"]) + """</span></div>
<div class="grid">""" + "\n".join(cards) + """</div><p class="meta" style="margin-top:28px">WorkBuddy：将官方历史导出文件放进桌面 Lyph 技术栈周更/workbuddy-exports，下次计划任务会读取。公网更新仍在站主后台确认后进行。</p></main></html>"""


def collect(config_path: Path) -> tuple[Path, Path]:
    config = json.loads(config_path.read_text("utf-8-sig"))
    roots = [Path(os.path.expandvars(value)).expanduser() for value in config.get("project_roots", [])]
    output_dir = Path(os.path.expandvars(config["output_dir"])).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=max(30, min(730, int(config.get("history_days", 365)))))

    projects = discover_projects(roots, int(config.get("max_depth", 7)))
    by_project: dict[int, set[str]] = {}
    for path, evidence in projects.items():
        for index in evidence:
            by_project.setdefault(index, set()).add(str(path))

    home = Path(os.path.expandvars(config.get("codex_home", "%USERPROFILE%/.codex"))).expanduser()
    codex, codex_files, codex_skipped = codex_mentions(home, cutoff)
    opencode, opencode_messages, opencode_status = opencode_mentions(
        Path(os.path.expandvars(config.get("opencode_database", "%USERPROFILE%/.local/share/opencode/opencode.db"))).expanduser(), cutoff,
    )
    workbuddy, workbuddy_files, workbuddy_skipped = workbuddy_mentions(
        Path(os.path.expandvars(config["workbuddy_exports"])).expanduser(), cutoff,
    )
    session_counts = {index: codex.get(index, 0) + opencode.get(index, 0) + workbuddy.get(index, 0) for index in range(len(CATALOG))}
    session_sources = {index: sum(bool(source.get(index)) for source in (codex, opencode, workbuddy)) for index in range(len(CATALOG))}

    published, baseline_status = fetch_published_names(config.get("public_site_url", ""))
    technologies = []
    for index, row in enumerate(CATALOG):
        name, icon, description, how, _aliases, _packages = row
        project_count = len(by_project.get(index, set()))
        mentions = session_counts.get(index, 0)
        if project_count == 0:
            continue
        # Match public names conservatively. Unknown baseline is never treated as "new".
        key = norm(name)
        alias_keys = {key, *(norm(alias) for alias in row[4]), *(norm(package) for package in row[5])}
        already_published = bool(alias_keys & published) if baseline_status == "ok" else None
        technologies.append({
            "name": name,
            "icon": icon,
            "description": description,
            "how": how,
            "suggested_progress": proficiency(project_count, session_sources.get(index, 0)),
            "project_count": project_count,
            "agent_text_units_with_mention": mentions,
            "agent_sources": [label for label, data in (("Codex", codex), ("OpenCode", opencode), ("WorkBuddy", workbuddy)) if data.get(index, 0)],
            "evidence_summary": f"{project_count} 个工程目录 · {mentions} 个 Agent 文本单元命中技术名",
            "already_published": already_published,
        })
    technologies.sort(key=lambda item: (-item["project_count"], item["name"].casefold()))
    report = {
        "schema_version": 1,
        "generated_at": now.astimezone().isoformat(timespec="seconds"),
        "status": "draft_only",
        "summary": {
            "projects_scanned": len(projects),
            "codex_files_scanned": codex_files,
            "codex_files_skipped": codex_skipped,
            "opencode_messages_scanned": opencode_messages,
            "opencode_status": opencode_status,
            "workbuddy_files_scanned": workbuddy_files,
            "workbuddy_files_skipped": workbuddy_skipped,
            "public_baseline_status": baseline_status,
            "history_days": max(30, min(730, int(config.get("history_days", 365)))),
            "raw_transcripts_uploaded": False,
        },
        "technologies": technologies,
        "session_only_observations": [
            {"name": row[0], "icon": row[1], "mentions": session_counts[index], "sources": [label for label, data in (("Codex", codex), ("OpenCode", opencode), ("WorkBuddy", workbuddy)) if data.get(index, 0)]}
            for index, row in enumerate(CATALOG) if session_counts.get(index, 0) and index not in by_project
        ],
    }
    stamp = now.strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"tech-stack-weekly-{stamp}.json"
    html_path = output_dir / f"tech-stack-weekly-{stamp}.html"
    latest_json = output_dir / "latest.json"
    latest_html = output_dir / "latest.html"
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    json_path.write_text(serialized, encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    latest_json.write_text(serialized, encoding="utf-8")
    latest_html.write_text(render_html(report), encoding="utf-8")
    keep = sorted(output_dir.glob("tech-stack-weekly-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in keep[12:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".html").unlink(missing_ok=True)
    return latest_json, latest_html


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--open-report", action="store_true", help="Open the local report when finished")
    args = parser.parse_args()
    try:
        json_path, html_path = collect(args.config)
    except Exception as error:
        print(f"Weekly stack collection failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "draft_ready", "json": str(json_path), "html": str(html_path)}, ensure_ascii=False))
    if args.open_report:
        webbrowser.open(html_path.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
