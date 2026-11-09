//! 技术栈周更采集服务。
//!
//! 这是 `tech_stack_collector.py` + `tech_stack_service.py` 的纯 Rust 移植：
//! 只在本机读取工程清单、Codex / OpenCode / WorkBuddy 会话记录，提取已知技术
//! 名称和聚合计数，绝不上传项目文件或对话原文。发布到公网仍是站主后台的独立
//! 人工步骤。

use anyhow::{anyhow, Result};
use chrono::{DateTime, Datelike, Duration as ChronoDuration, Local, NaiveTime, TimeZone, Utc};
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::thread::JoinHandle;
use std::time::{Duration, SystemTime};

use crate::logs::EventLog;

const MAX_MANIFEST_BYTES: u64 = 2_000_000;
const MAX_SESSION_BYTES: u64 = 40_000_000;
const MAX_KEEP_REPORTS: usize = 12;

/// 一条技术目录项：(名称, 图标, 用途, 如何使用, 别名, 包名)。
type CatalogRow = (
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    &'static [&'static str],
    &'static [&'static str],
);

/// 与 Python `CATALOG` 逐字对应，顺序也保持一致（索引即技术 ID）。
const CATALOG: &[CatalogRow] = &[
    ("TypeScript", "🟦", "JavaScript 的静态类型扩展，用于在开发阶段发现接口和数据结构问题。", "为 API 响应、组件属性和业务对象定义类型；用 tsc 检查，再由构建工具编译。", &["typescript"], &["typescript"]),
    ("JavaScript", "🟨", "浏览器与 Node.js 常用脚本语言，用于页面交互、工具脚本和服务端应用。", "用 ES modules 组织功能，优先拆分纯函数，再通过浏览器或 Node.js 运行。", &["javascript", "node.js"], &["javascript"]),
    ("React", "⚛️", "组件化 UI 库，用状态和属性组合可复用的交互界面。", "把页面拆为小组件，用 props 传数据、用 state 管理交互，并保持渲染逻辑可预测。", &["react"], &["react"]),
    ("Next.js", "▲", "基于 React 的 Web 框架，提供路由、服务端渲染和生产构建。", "按路由组织页面；将需要密钥的请求放在服务端，将交互状态留在客户端组件。", &["next.js", "nextjs"], &["next"]),
    ("Vue", "🟩", "渐进式前端框架，适合构建组件化交互页面。", "使用单文件组件、响应式状态和 props/events 拆分页面功能。", &["vue.js", "vue"], &["vue"]),
    ("Vite", "⚡", "前端开发服务器与打包工具，提供快速热更新和生产构建。", "用插件接入框架，开发时运行 dev server，发布前执行生产 build。", &["vite"], &["vite"]),
    ("Tailwind CSS", "🎨", "实用优先的 CSS 工具集，通过组合样式类构建页面布局。", "把间距、布局、响应式和状态样式放在组件 class 中，重复模式再抽成组件。", &["tailwind css", "tailwindcss"], &["tailwindcss"]),
    ("shadcn/ui", "🧩", "基于 Radix 与 Tailwind 的可定制 React 组件方案。", "选择需要的组件源码放入项目，按产品设计修改，不把组件库当成不可改的黑盒。", &["shadcn/ui", "shadcn ui"], &["shadcn"]),
    ("Framer Motion", "🎞️", "React 动画库，用于声明式过渡、布局动画和手势交互。", "为状态切换配置短时长过渡，减少大面积模糊和影响滚动的动画。", &["framer motion", "motion/react"], &["framer-motion", "motion"]),
    ("Java", "☕", "面向对象语言，常用于后端服务、企业应用和数据处理工具。", "用清晰的领域对象组织业务逻辑，依赖构建工具编译，并以单元测试固定行为。", &["java"], &["java"]),
    ("Spring Boot", "🍃", "Java 服务端框架，简化 Web API、配置和依赖管理。", "按 Controller、Service、Repository 分层；用配置文件管理环境差异和健康检查。", &["spring boot", "springboot"], &["spring-boot-starter", "spring-boot"]),
    ("Maven", "📦", "Java 项目构建和依赖管理工具。", "在 pom.xml 锁定依赖，通过 Maven 生命周期编译、测试和打包。", &["maven"], &["maven"]),
    ("Gradle", "🛠️", "基于任务图的构建工具，常用于 Java、Kotlin 和 Android。", "用 build.gradle 声明插件、依赖与任务，使用 wrapper 固定项目构建版本。", &["gradle"], &["gradle"]),
    ("Python", "🐍", "通用编程语言，常用于 API、自动化、数据分析与机器学习。", "按功能拆模块，用虚拟环境隔离依赖，并通过类型提示和自动化检查维护代码。", &["python"], &["python"]),
    ("FastAPI", "🚀", "基于 Python 类型标注构建异步 HTTP API 的框架。", "使用 Pydantic 模型校验输入，路由保持薄层，外部 I/O 设置超时并明确错误。", &["fastapi"], &["fastapi"]),
    ("Flask", "🌶️", "轻量 Python Web 框架，适合小型服务和可组合的 HTTP 应用。", "以蓝图划分路由，将业务逻辑抽到服务层，并在应用工厂中管理配置。", &["flask"], &["flask"]),
    ("Django", "🎸", "包含 ORM、路由和管理界面的 Python Web 框架。", "用模型表达数据关系，表单和视图校验输入，并通过迁移管理数据库结构。", &["django"], &["django"]),
    ("Pydantic", "✅", "Python 数据校验和序列化库，常用于 API 输入输出模型。", "为外部输入定义模型和约束，避免业务函数重复解析不可信字典。", &["pydantic"], &["pydantic"]),
    ("SQLAlchemy", "🗃️", "Python SQL 工具包和 ORM，支持声明式模型及显式 SQL。", "为事务边界和连接生命周期建立上下文，复杂查询保留可审查的 SQL 语义。", &["sqlalchemy"], &["sqlalchemy"]),
    ("LangChain", "🔗", "用于组合模型、提示、检索器和工具的应用框架。", "将检索、工具调用和答案生成拆成可观测步骤，并为权限与错误设边界。", &["langchain"], &["langchain"]),
    ("MCP", "🔌", "连接 AI 客户端与外部工具、资源的开放协议。", "公开最小只读工具集合，校验每个参数与调用身份，不提供任意 SQL 或 shell。", &["model context protocol", "mcp"], &["mcp"]),
    ("OpenAI 兼容 API", "🤖", "遵循 OpenAI 风格请求格式的模型 API 接口。", "将端点、模型和密钥置于服务端配置，客户端只发送业务输入，不暴露凭证。", &["openai compatible", "openai api"], &["openai"]),
    ("SQLite", "🪶", "嵌入式关系数据库，适合单机应用和低运维数据存储。", "用事务保护多步写入，启用 WAL 时按 SQLite 备份规则处理数据库与日志。", &["sqlite"], &["sqlite"]),
    ("sqlite-vec", "🧭", "SQLite 向量搜索扩展，可将相似度检索放在本地数据库中。", "为向量表保存模型版本与维度，检索后仍按权限、版本和条件校验结果。", &["sqlite-vec", "sqlite vec"], &["sqlite-vec"]),
    ("MySQL", "🐬", "常见关系数据库，适用于多用户业务数据和事务型查询。", "为过滤与关联列建索引，使用参数化 SQL 和明确的事务隔离边界。", &["mysql"], &["mysql-connector", "pymysql", "mysqlclient"]),
    ("PostgreSQL", "🐘", "功能丰富的关系数据库，支持事务、索引和 JSON 数据。", "以迁移管理 schema，使用参数化查询并按实际查询计划优化索引。", &["postgresql", "postgres"], &["psycopg", "asyncpg"]),
    ("Redis", "🔴", "内存数据结构服务，常用于缓存、限流和短期协调。", "为 key 设计过期策略和命名空间；缓存失效时要保证核心请求仍能降级。", &["redis"], &["redis", "ioredis"]),
    ("Elasticsearch", "🔎", "分布式全文检索与聚合引擎。", "明确 mapping、分词和索引版本；索引切换使用别名并保留回滚路径。", &["elasticsearch"], &["elasticsearch", "elasticsearch-py"]),
    ("Neo4j", "🕸️", "属性图数据库，适合表达实体及多跳关系。", "先定义节点与关系类型，再以参数化 Cypher 限制查询深度和结果规模。", &["neo4j"], &["neo4j"]),
    ("PyTorch", "🔥", "Python 深度学习框架，支持张量计算和自动微分。", "固定随机种子与数据划分，分别记录训练配置、权重和评估指标。", &["pytorch", "torch"], &["torch"]),
    ("NumPy", "🔢", "Python 数值计算基础库，提供多维数组和向量化运算。", "用数组运算替代逐项 Python 循环，明确 dtype、维度和缺失值处理。", &["numpy"], &["numpy"]),
    ("pandas", "🐼", "Python 表格数据处理库，用于清洗、连接和汇总数据。", "先检查列类型与缺失值，再进行 groupby/merge，并在导出前验证行数与口径。", &["pandas"], &["pandas"]),
    ("scikit-learn", "🧪", "Python 机器学习工具包，提供预处理、传统模型和评估方法。", "用 Pipeline 固定预处理与模型步骤，按正确的数据划分评估并记录随机种子。", &["scikit-learn", "scikit learn"], &["scikit-learn"]),
    ("Jupyter", "📓", "交互式笔记本环境，适合探索、可视化和记录分析过程。", "将探索笔记转成可重复执行的步骤，保留数据来源、环境和结果解释。", &["jupyter"], &["jupyter", "ipykernel"]),
    ("pyecharts", "📊", "Python 图表库，可生成交互式 ECharts 可视化。", "统一数据口径和图表选项，输出前检查空值、单位、图例与移动端尺寸。", &["pyecharts"], &["pyecharts"]),
    ("Rust", "🦀", "强调内存安全和性能的系统编程语言。", "用类型表达状态与错误，通过 Cargo 管理依赖，并优先采用安全 Rust。", &["rust"], &["rust"]),
    ("Tauri", "🪟", "使用系统 WebView 与 Rust 后端构建轻量桌面应用的框架。", "只向前端开放必要 commands 和 capabilities，限制文件与进程权限。", &["tauri"], &["tauri"]),
    ("C++", "⚙️", "通用系统与高性能应用语言，支持面向对象和泛型编程。", "明确所有权与资源生命周期，使用构建系统并以 sanitizers/测试检查边界。", &["c++", "cpp"], &["c++"]),
    ("C", "🔧", "底层过程式语言，常用于系统、嵌入式和性能敏感组件。", "显式检查缓冲区长度和返回码，集中管理资源释放并运行静态分析。", &[" c ", "语言 c"], &["c"]),
    ("Go", "🐹", "带有内置并发和部署工具链的编译型语言。", "用 context 传递取消与超时，接口保持小型，运行 go test 和 go vet。", &["golang", "go language"], &["go"]),
    ("Docker", "🐳", "容器镜像与运行环境工具，用于封装和交付服务。", "使用多阶段构建、非 root 用户和固定镜像版本，容器只挂载必要目录。", &["docker"], &["docker"]),
    ("Docker Compose", "📦", "以 YAML 描述多容器应用及其网络、卷和健康检查。", "将服务依赖和健康检查写进 compose，部署前验证配置并使用受控卷。", &["docker compose", "compose.yaml"], &["docker compose"]),
    ("Nginx", "🌐", "反向代理和 Web 服务器，可处理 TLS、路由和静态资源。", "按路径设置代理和安全头，部署前运行 nginx -t，变更后核对外部健康检查。", &["nginx"], &["nginx"]),
    ("MinIO", "🪣", "兼容 S3 API 的对象存储服务，可管理文件与备份。", "将对象 bucket 设为私有，服务端使用最小权限凭证，并验证恢复链路。", &["minio"], &["minio"]),
    ("Git", "🌱", "分布式版本控制工具，用于追踪源码变化和回滚。", "按可审查的主题提交变更，提交前检查 diff、忽略敏感文件并记录回滚点。", &["git"], &["git"]),
    ("Playwright", "🎭", "浏览器自动化与端到端测试框架。", "用稳定角色或标签定位元素，等待可观察状态，并隔离真实用户数据。", &["playwright"], &["playwright"]),
    ("pytest", "🧫", "Python 测试框架，支持 fixture 和参数化测试。", "测试公开输入输出和失败路径，fixture 管理隔离资源并保证 teardown。", &["pytest"], &["pytest"]),
    ("k6", "📈", "用 JavaScript 编写负载场景的性能测试工具。", "先定义用户场景和阈值，再逐级加压并把失败数、延迟和资源指标一起记录。", &["k6"], &["k6"]),
    ("GitHub Actions", "🐙", "GitHub 托管的 CI/CD 工作流服务。", "为构建和检查设最小权限，固定 action 版本，secret 只通过受保护变量注入。", &["github actions"], &["github-actions"]),
    ("APScheduler", "⏰", "Python 进程内定时任务调度库。", "为任务设时区、幂等键和失败状态；多副本场景需避免重复调度。", &["apscheduler"], &["apscheduler"]),
    ("LangChain4j", "🦜", "Java 生态的 LLM、工具和检索应用开发库。", "通过接口封装模型和检索器，工具参数设白名单并记录来源版本。", &["langchain4j"], &["langchain4j"]),
    ("OpenCV", "👁️", "跨平台图像处理与计算机视觉库。", "统一颜色空间与尺寸，对文件输入做容量限制，并用小样本确认处理结果。", &["opencv", "cv2"], &["opencv-python", "opencv"]),
    ("WeChat Mini Program", "💬", "微信小程序平台，用页面、组件和平台 API 构建轻量应用。", "遵循小程序页面生命周期，敏感能力先校验用户授权，并验证真机布局。", &["wechat mini program", "微信小程序", "miniprogram"], &["miniprogram"]),
    ("Codex", "🧠", "OpenAI 的编码代理，可在本地会话中协助分析、修改和验证项目。", "按项目说明给出明确任务，审阅文件改动与命令结果，再决定是否合并或发布。", &["codex", "codex cli", "openai codex"], &[]),
    ("OpenCode", "🧰", "可连接多种模型的开源编码代理，保存本地项目会话。", "选择模型和项目目录后，通过会话指令完成代码探索；发布前审阅变更与权限。", &["opencode"], &[]),
    ("WorkBuddy", "🐧", "腾讯的桌面 AI Agent，可围绕本地工作区完成多步骤任务。", "把任务限定到目标工作区，检查它拟读写的文件，并在应用历史中复核交付。", &["workbuddy"], &[]),
];

/// 与 Python `IGNORED_DIRS` 逐字一致（比较时统一转小写）。
const IGNORED_DIRS: &[&str] = &[
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".next",
    ".turbo",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "target",
    "vendor",
    "coverage",
    "__pycache__",
    ".pytest_cache",
    "site-packages",
    "packages",
    "bower_components",
    ".idea",
];

const MANIFESTS: &[&str] = &[
    "package.json",
    "pyproject.toml",
    "cargo.toml",
    "pom.xml",
    "go.mod",
    "dockerfile",
];

const SUFFIX_TECH: &[(&str, &str)] = &[
    (".py", "Python"),
    (".java", "Java"),
    (".rs", "Rust"),
    (".go", "Go"),
    (".ts", "TypeScript"),
    (".tsx", "TypeScript"),
    (".js", "JavaScript"),
    (".jsx", "JavaScript"),
    (".cpp", "C++"),
    (".cc", "C++"),
    (".c", "C"),
    (".ipynb", "Jupyter"),
];

const TEXT_KEYS: &[&str] = &["text", "message", "content", "input", "output"];

// ---------------------------------------------------------------------------
// 基础工具
// ---------------------------------------------------------------------------

/// 与 Python `norm` 一致：小写后去掉所有非 `[a-z0-9]` 字符。
fn norm(value: &str) -> String {
    value
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect()
}

/// 与 Python `package_name` 一致：去空白、转小写、首个 `[<=>!~;\[]` 之前、最后一个 `/` 之后。
fn package_name(value: &str) -> String {
    let lowered = value.trim().to_lowercase();
    let cut = lowered
        .find(|c: char| matches!(c, '<' | '=' | '>' | '!' | '~' | ';' | '['))
        .map(|index| &lowered[..index])
        .unwrap_or(lowered.as_str());
    cut.rsplit('/').next().unwrap_or("").to_string()
}

fn catalog_index(name: &str) -> Option<usize> {
    CATALOG.iter().position(|row| row.0 == name)
}

fn is_ignored_dir(name: &str) -> bool {
    let lower = name.to_lowercase();
    IGNORED_DIRS.contains(&lower.as_str())
}

fn strip_bom(text: &str) -> &str {
    text.strip_prefix('\u{feff}').unwrap_or(text)
}

fn is_plain_alias(alias: &str) -> bool {
    !alias.is_empty()
        && alias.chars().all(|c| {
            c.is_ascii_alphanumeric() || matches!(c, '.' | '+' | '#' | '/' | '_' | ' ' | '-')
        })
}

fn home_dir() -> PathBuf {
    for key in ["USERPROFILE", "HOME"] {
        if let Some(value) = std::env::var_os(key) {
            if !value.is_empty() {
                return PathBuf::from(value);
            }
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

/// 展开 `%VAR%`、`${VAR}`、`$VAR`，与 Python `os.path.expandvars` 的主要行为一致。
fn expand_env_vars(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut index = 0usize;
    while index < chars.len() {
        let current = chars[index];
        if current == '%' {
            if let Some(end) = (index + 1..chars.len()).find(|&pos| chars[pos] == '%') {
                let name: String = chars[index + 1..end].iter().collect();
                if !name.is_empty() {
                    if let Some(value) = std::env::var_os(name.as_str()) {
                        out.push_str(&value.to_string_lossy());
                        index = end + 1;
                        continue;
                    }
                }
            }
        } else if current == '$' {
            if index + 1 < chars.len() && chars[index + 1] == '{' {
                if let Some(end) = (index + 2..chars.len()).find(|&pos| chars[pos] == '}') {
                    let name: String = chars[index + 2..end].iter().collect();
                    if let Some(value) = std::env::var_os(name.as_str()) {
                        out.push_str(&value.to_string_lossy());
                        index = end + 1;
                        continue;
                    }
                }
            } else {
                let mut end = index + 1;
                while end < chars.len() && (chars[end].is_ascii_alphanumeric() || chars[end] == '_') {
                    end += 1;
                }
                if end > index + 1 {
                    let name: String = chars[index + 1..end].iter().collect();
                    if let Some(value) = std::env::var_os(name.as_str()) {
                        out.push_str(&value.to_string_lossy());
                        index = end;
                        continue;
                    }
                }
            }
        }
        out.push(current);
        index += 1;
    }
    out
}

fn expand_path(value: &str) -> PathBuf {
    let expanded = expand_env_vars(value.trim());
    if expanded == "~" {
        return home_dir();
    }
    if let Some(rest) = expanded
        .strip_prefix("~/")
        .or_else(|| expanded.strip_prefix("~\\"))
    {
        return home_dir().join(rest);
    }
    PathBuf::from(expanded)
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64().map(|v| v != 0.0).unwrap_or(false),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// 近似 Python `str(value)`（只覆盖本项目会出现的标量）。
fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        other => other.to_string(),
    }
}

/// 近似 Python `str(value or "")`。
fn py_str_or_empty(value: &Value) -> String {
    if python_truthy(value) {
        py_str(value)
    } else {
        String::new()
    }
}

/// 近似 Python `int(value)`，失败返回 None。
fn json_to_i64(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|v| v as i64)),
        Value::String(text) => text.trim().parse::<i64>().ok(),
        Value::Bool(flag) => Some(if *flag { 1 } else { 0 }),
        _ => None,
    }
}

fn suffix_of(path: &Path) -> String {
    path.extension()
        .map(|ext| format!(".{}", ext.to_string_lossy().to_lowercase()))
        .unwrap_or_default()
}

fn escape_html(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            other => out.push(other),
        }
    }
    out
}

fn read_text_file(path: &Path) -> Option<String> {
    let bytes = fs::read(path).ok()?;
    let text = String::from_utf8_lossy(&bytes).to_string();
    Some(strip_bom(&text).to_string())
}

fn read_json_manifest(path: &Path) -> Option<Value> {
    let size = fs::metadata(path).ok()?.len();
    if size > MAX_MANIFEST_BYTES {
        return None;
    }
    let text = read_text_file(path)?;
    serde_json::from_str(&text).ok()
}

// ---------------------------------------------------------------------------
// 目录索引与别名匹配
// ---------------------------------------------------------------------------

struct TermPattern {
    regex: Regex,
    bounded: bool,
    index: usize,
}

struct CatalogIndexes {
    by_package: HashMap<String, usize>,
    patterns: Vec<TermPattern>,
}

fn catalog_indexes() -> &'static CatalogIndexes {
    static INDEXES: OnceLock<CatalogIndexes> = OnceLock::new();
    INDEXES.get_or_init(|| {
        let mut by_package: HashMap<String, usize> = HashMap::new();
        let mut patterns: Vec<TermPattern> = Vec::new();
        for (index, row) in CATALOG.iter().enumerate() {
            for package in row.5 {
                by_package.insert(package_name(package), index);
            }
            for alias in row.4 {
                let escaped = regex::escape(alias.trim());
                if escaped.is_empty() {
                    continue;
                }
                let bounded = if alias.starts_with(' ') || alias.ends_with(' ') {
                    false
                } else if is_plain_alias(alias) {
                    true
                } else {
                    // Python 里既没首尾空格、也不匹配 `[A-Za-z0-9.+#/_ -]+` 的别名会被丢掉。
                    continue;
                };
                let pattern = format!("(?i){escaped}");
                if let Ok(regex) = Regex::new(&pattern) {
                    patterns.push(TermPattern {
                        regex,
                        bounded,
                        index,
                    });
                }
            }
        }
        CatalogIndexes {
            by_package,
            patterns,
        }
    })
}

fn has_bounded_match(regex: &Regex, text: &str) -> bool {
    for matched in regex.find_iter(text) {
        let before_ok = text[..matched.start()]
            .chars()
            .next_back()
            .map(|c| !c.is_ascii_alphanumeric())
            .unwrap_or(true);
        let after_ok = text[matched.end()..]
            .chars()
            .next()
            .map(|c| !c.is_ascii_alphanumeric())
            .unwrap_or(true);
        if before_ok && after_ok {
            return true;
        }
    }
    false
}

fn secret_regex() -> &'static Regex {
    static SECRET: OnceLock<Regex> = OnceLock::new();
    SECRET.get_or_init(|| {
        Regex::new(
            r"(?i)(api[_ -]?key|access[_ -]?token|password|passwd|secret|authorization|-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9]{20,})",
        )
        .expect("内置 SECRET_PATTERN 一定合法")
    })
}

fn artifact_regex() -> &'static Regex {
    static ARTIFACT: OnceLock<Regex> = OnceLock::new();
    ARTIFACT.get_or_init(|| {
        Regex::new(r"<artifactId>([^<]*)</artifactId>")
            .expect("内置 artifactId 正则一定合法")
    })
}

/// 与 Python `technology_mentions` 一致：命中密钥模式时整段忽略。
fn technology_mentions(text: &str) -> BTreeSet<usize> {
    let mut found = BTreeSet::new();
    if text.is_empty() || secret_regex().is_match(text) {
        return found;
    }
    for pattern in &catalog_indexes().patterns {
        let hit = if pattern.bounded {
            has_bounded_match(&pattern.regex, text)
        } else {
            pattern.regex.is_match(text)
        };
        if hit {
            found.insert(pattern.index);
        }
    }
    found
}

// ---------------------------------------------------------------------------
// 工程证据
// ---------------------------------------------------------------------------

fn add_package_name_str(value: &str, found: &mut BTreeSet<usize>) {
    if let Some(index) = catalog_indexes().by_package.get(&package_name(value)) {
        found.insert(*index);
    }
}

fn add_package_names(value: &Value, found: &mut BTreeSet<usize>) {
    match value {
        Value::String(text) => add_package_name_str(text, found),
        Value::Object(map) => {
            for key in map.keys() {
                add_package_name_str(key, found);
            }
        }
        Value::Array(items) => {
            for item in items {
                add_package_names(item, found);
            }
        }
        _ => {}
    }
}

fn add_package_names_toml(value: &toml::Value, found: &mut BTreeSet<usize>) {
    match value {
        toml::Value::String(text) => add_package_name_str(text, found),
        toml::Value::Table(table) => {
            for key in table.keys() {
                add_package_name_str(key, found);
            }
        }
        toml::Value::Array(items) => {
            for item in items {
                add_package_names_toml(item, found);
            }
        }
        _ => {}
    }
}

fn add_tech(found: &mut BTreeSet<usize>, name: &str) {
    if let Some(index) = catalog_index(name) {
        found.insert(index);
    }
}

fn count_subtree_suffixes(root: &Path, counts: &mut HashMap<String, usize>) {
    let mut stack: Vec<PathBuf> = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = match fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        let mut files = 0usize;
        for entry in entries.flatten() {
            let path = entry.path();
            let meta = match fs::metadata(&path) {
                Ok(meta) => meta,
                Err(_) => continue,
            };
            if meta.is_dir() {
                if !is_ignored_dir(&entry.file_name().to_string_lossy()) {
                    stack.push(path);
                }
            } else if meta.is_file() {
                if files >= 2000 {
                    continue;
                }
                files += 1;
                *counts.entry(suffix_of(&path)).or_insert(0) += 1;
            }
        }
    }
}

/// 与 Python `project_evidence` 一致，返回命中的 CATALOG 索引集合。
fn project_evidence(project: &Path) -> BTreeSet<usize> {
    let mut found: BTreeSet<usize> = BTreeSet::new();
    let entries = match fs::read_dir(project) {
        Ok(entries) => entries,
        Err(_) => return found,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let meta = match fs::metadata(&path) {
            Ok(meta) => meta,
            Err(_) => continue,
        };
        if !meta.is_file() || meta.len() > MAX_MANIFEST_BYTES {
            continue;
        }
        let lower = entry.file_name().to_string_lossy().to_lowercase();
        match lower.as_str() {
            "package.json" => {
                if let Some(Value::Object(data)) = read_json_manifest(&path) {
                    for group in [
                        "dependencies",
                        "devDependencies",
                        "peerDependencies",
                        "optionalDependencies",
                    ] {
                        if let Some(value) = data.get(group) {
                            add_package_names(value, &mut found);
                        }
                    }
                    if ["scripts", "dependencies", "devDependencies"]
                        .iter()
                        .any(|key| data.contains_key(*key))
                    {
                        add_tech(&mut found, "JavaScript");
                    }
                }
            }
            "pyproject.toml" => {
                let text = match read_text_file(&path) {
                    Some(text) => text,
                    None => continue,
                };
                let data: toml::Value = match toml::from_str(&text) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                if let Some(project_table) = data.get("project") {
                    if let Some(deps) = project_table.get("dependencies") {
                        add_package_names_toml(deps, &mut found);
                    }
                    if let Some(optional) = project_table.get("optional-dependencies") {
                        add_package_names_toml(optional, &mut found);
                    }
                }
                if let Some(tool) = data.get("tool") {
                    for manager in ["poetry", "uv", "pdm", "hatch"] {
                        if let Some(value) = tool.get(manager) {
                            add_package_names_toml(value, &mut found);
                        }
                    }
                }
                if data.get("project").is_some() {
                    add_tech(&mut found, "Python");
                }
            }
            "cargo.toml" => {
                let text = match read_text_file(&path) {
                    Some(text) => text,
                    None => continue,
                };
                let data: toml::Value = match toml::from_str(&text) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                for key in ["dependencies", "dev-dependencies", "build-dependencies"] {
                    if let Some(value) = data.get(key) {
                        add_package_names_toml(value, &mut found);
                    }
                }
                add_tech(&mut found, "Rust");
            }
            "pom.xml" => {
                if let Some(text) = read_text_file(&path) {
                    for capture in artifact_regex().captures_iter(&text) {
                        if let Some(inner) = capture.get(1) {
                            add_package_name_str(inner.as_str(), &mut found);
                        }
                    }
                    add_tech(&mut found, "Java");
                }
            }
            "go.mod" => add_tech(&mut found, "Go"),
            "dockerfile" => add_tech(&mut found, "Docker"),
            _ => {}
        }
        if lower.starts_with("requirements") && lower.ends_with(".txt") {
            if let Some(text) = read_text_file(&path) {
                for line in text.lines() {
                    let line = line.trim();
                    if line.is_empty()
                        || line.starts_with('#')
                        || line.starts_with('-')
                        || line.starts_with("http://")
                        || line.starts_with("https://")
                    {
                        continue;
                    }
                    add_package_name_str(line, &mut found);
                }
            }
        }
    }

    // 源码后缀是语言使用的独立证据。
    let mut suffix_counts: HashMap<String, usize> = HashMap::new();
    if let Ok(entries) = fs::read_dir(project) {
        for entry in entries.flatten() {
            let path = entry.path();
            let meta = match fs::metadata(&path) {
                Ok(meta) => meta,
                Err(_) => continue,
            };
            if meta.is_file() && meta.len() <= MAX_MANIFEST_BYTES {
                *suffix_counts.entry(suffix_of(&path)).or_insert(0) += 1;
            } else if meta.is_dir() {
                let name = entry.file_name().to_string_lossy().to_string();
                if matches!(name.as_str(), "src" | "app" | "lib" | "tests" | "test") {
                    count_subtree_suffixes(&path, &mut suffix_counts);
                }
            }
        }
    }
    for (ext, name) in SUFFIX_TECH {
        if suffix_counts.get(*ext).copied().unwrap_or(0) > 0 {
            add_tech(&mut found, *name);
        }
    }
    found
}

/// 与 Python `discover_projects` 一致：深度受限递归，命中清单才算工程。
fn discover_projects(roots: &[PathBuf], max_depth: i64) -> BTreeMap<PathBuf, BTreeSet<usize>> {
    let mut projects: BTreeMap<PathBuf, BTreeSet<usize>> = BTreeMap::new();
    for root in roots {
        if !root.is_dir() {
            continue;
        }
        let root = fs::canonicalize(root).unwrap_or_else(|_| root.clone());
        let mut stack: Vec<(PathBuf, i64)> = vec![(root, 0)];
        while let Some((dir, depth)) = stack.pop() {
            let entries = match fs::read_dir(&dir) {
                Ok(entries) => entries,
                Err(_) => continue,
            };
            let mut has_manifest = false;
            for entry in entries.flatten() {
                let path = entry.path();
                let meta = match fs::metadata(&path) {
                    Ok(meta) => meta,
                    Err(_) => continue,
                };
                let name = entry.file_name().to_string_lossy().to_string();
                if meta.is_dir() {
                    if depth < max_depth
                        && !is_ignored_dir(&name)
                        && !name.starts_with('.')
                    {
                        stack.push((path, depth + 1));
                    }
                } else if meta.is_file() {
                    let lower = name.to_lowercase();
                    if MANIFESTS.contains(&lower.as_str())
                        || (lower.starts_with("requirements") && lower.ends_with(".txt"))
                    {
                        has_manifest = true;
                    }
                }
            }
            if has_manifest {
                let evidence = project_evidence(&dir);
                if !evidence.is_empty() {
                    projects.insert(dir, evidence);
                }
            }
        }
    }
    projects
}

// ---------------------------------------------------------------------------
// 会话证据
// ---------------------------------------------------------------------------

/// 与 Python `text_fragments` 一致：按 key 名递归收集字符串片段。
fn collect_fragments(value: &Value, key: &str, out: &mut Vec<String>) {
    match value {
        Value::String(text) => {
            let lower = key.to_lowercase();
            if TEXT_KEYS.contains(&lower.as_str()) {
                out.push(text.clone());
            }
        }
        Value::Array(items) => {
            for item in items {
                collect_fragments(item, key, out);
            }
        }
        Value::Object(map) => {
            for (child_key, child) in map {
                collect_fragments(child, child_key, out);
            }
        }
        _ => {}
    }
}

fn walk_files<F>(root: &Path, predicate: F) -> Vec<PathBuf>
where
    F: Fn(&Path) -> bool,
{
    let mut out = Vec::new();
    let mut stack: Vec<PathBuf> = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = match fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let meta = match fs::metadata(&path) {
                Ok(meta) => meta,
                Err(_) => continue,
            };
            if meta.is_dir() {
                stack.push(path);
            } else if meta.is_file() && predicate(&path) {
                out.push(path);
            }
        }
    }
    out
}

fn is_jsonl(path: &Path) -> bool {
    path.extension()
        .map(|ext| ext.to_string_lossy().eq_ignore_ascii_case("jsonl"))
        .unwrap_or(false)
}

fn codex_mentions(
    codex_home: &Path,
    cutoff: DateTime<Utc>,
) -> (HashMap<usize, usize>, u64, u64) {
    let mut mentions: HashMap<usize, usize> = HashMap::new();
    let mut files_seen = 0u64;
    let mut files_skipped = 0u64;
    for sub in ["sessions", "archived_sessions"] {
        let root = codex_home.join(sub);
        if !root.is_dir() {
            continue;
        }
        for path in walk_files(&root, is_jsonl) {
            let meta = match fs::metadata(&path) {
                Ok(meta) => meta,
                Err(_) => {
                    files_skipped += 1;
                    continue;
                }
            };
            let modified = meta
                .modified()
                .map(|time| DateTime::<Utc>::from(time))
                .unwrap_or(DateTime::<Utc>::MIN_UTC);
            if modified < cutoff || meta.len() > MAX_SESSION_BYTES {
                continue;
            }
            files_seen += 1;
            let file = match fs::File::open(&path) {
                Ok(file) => file,
                Err(_) => {
                    files_skipped += 1;
                    continue;
                }
            };
            let mut found: BTreeSet<usize> = BTreeSet::new();
            for line in BufReader::new(file).lines() {
                let line = match line {
                    Ok(line) => line,
                    Err(_) => break,
                };
                if line.trim().is_empty() {
                    continue;
                }
                let event: Value = match serde_json::from_str(&line) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                let payload = match event.get("payload").filter(|value| value.is_object()) {
                    Some(payload) => payload,
                    None => continue,
                };
                let role = payload.get("role").and_then(|value| value.as_str()).unwrap_or("");
                let kind = payload.get("type").and_then(|value| value.as_str()).unwrap_or("");
                if !matches!(role, "user" | "assistant")
                    && !matches!(kind, "user_message" | "agent_message")
                {
                    continue;
                }
                let mut fragments: Vec<String> = Vec::new();
                collect_fragments(payload, "", &mut fragments);
                for fragment in fragments {
                    found.extend(technology_mentions(&fragment));
                }
            }
            for index in found {
                *mentions.entry(index).or_insert(0) += 1;
            }
        }
    }
    (mentions, files_seen, files_skipped)
}

fn value_ref_string(value: rusqlite::types::ValueRef<'_>) -> Option<String> {
    use rusqlite::types::ValueRef;
    match value {
        ValueRef::Null => None,
        ValueRef::Integer(number) => Some(number.to_string()),
        ValueRef::Real(number) => Some(number.to_string()),
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => {
            Some(String::from_utf8_lossy(bytes).to_string())
        }
    }
}

fn opencode_mentions(
    database: &Path,
    cutoff: DateTime<Utc>,
) -> (HashMap<usize, usize>, u64, String) {
    let mut mentions: HashMap<usize, usize> = HashMap::new();
    if !database.exists() {
        return (mentions, 0, "not_found".to_string());
    }
    let connection = match rusqlite::Connection::open_with_flags(
        database,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    ) {
        Ok(connection) => connection,
        Err(_) => return (mentions, 0, "readonly_unavailable".to_string()),
    };
    // 与 Python sqlite3.connect(timeout=2) 的等待策略保持一致。
    let _ = connection.busy_timeout(Duration::from_secs(2));

    let mut tables: HashSet<String> = HashSet::new();
    match connection.prepare("SELECT name FROM sqlite_master WHERE type='table'") {
        Ok(mut statement) => match statement.query_map(rusqlite::params![], |row| row.get::<_, String>(0)) {
            Ok(rows) => {
                for row in rows.flatten() {
                    tables.insert(row);
                }
            }
            Err(_) => return (mentions, 0, "readonly_unavailable".to_string()),
        },
        Err(_) => return (mentions, 0, "readonly_unavailable".to_string()),
    }
    if !tables.contains("message") || !tables.contains("part") {
        return (mentions, 0, "schema_unsupported".to_string());
    }
    let _ = connection.execute_batch("PRAGMA query_only=ON");

    let cutoff_ms = cutoff.timestamp_millis();
    let mut statement = match connection.prepare(
        "SELECT m.id, m.data, p.data FROM message AS m JOIN part AS p ON p.message_id=m.id \
         WHERE m.time_created >= ?1 AND p.time_created >= ?1 ORDER BY m.id",
    ) {
        Ok(statement) => statement,
        Err(_) => return (mentions, 0, "readonly_unavailable".to_string()),
    };
    let mut rows = match statement.query(rusqlite::params![cutoff_ms]) {
        Ok(rows) => rows,
        Err(_) => return (mentions, 0, "readonly_unavailable".to_string()),
    };

    let mut scanned = 0u64;
    let mut current_id: Option<String> = None;
    let mut current_role: Option<String> = None;
    let mut current_found: BTreeSet<usize> = BTreeSet::new();
    loop {
        let row = match rows.next() {
            Ok(Some(row)) => row,
            Ok(None) => break,
            Err(_) => break,
        };
        let id = row
            .get_ref(0)
            .ok()
            .and_then(value_ref_string)
            .unwrap_or_default();
        let message_data = row.get_ref(1).ok().and_then(value_ref_string);
        let part_data = row.get_ref(2).ok().and_then(value_ref_string);

        if current_id.as_deref() != Some(id.as_str()) {
            if current_id.is_some() {
                if matches!(current_role.as_deref(), Some("user") | Some("assistant")) {
                    for index in &current_found {
                        *mentions.entry(*index).or_insert(0) += 1;
                    }
                    scanned += 1;
                }
                current_found.clear();
            }
            current_id = Some(id);
            current_role = None;
            if let Some(text) = message_data {
                if let Ok(envelope) = serde_json::from_str::<Value>(&text) {
                    current_role = envelope
                        .get("role")
                        .and_then(|value| value.as_str())
                        .map(|value| value.to_string());
                }
            }
        }

        if !matches!(current_role.as_deref(), Some("user") | Some("assistant")) {
            continue;
        }
        if let Some(text) = part_data {
            if let Ok(data) = serde_json::from_str::<Value>(&text) {
                if data.get("type").and_then(|value| value.as_str()) == Some("text") {
                    if let Some(body) = data.get("text").and_then(|value| value.as_str()) {
                        current_found.extend(technology_mentions(body));
                    }
                }
            }
        }
    }
    if current_id.is_some()
        && matches!(current_role.as_deref(), Some("user") | Some("assistant"))
    {
        for index in &current_found {
            *mentions.entry(*index).or_insert(0) += 1;
        }
        scanned += 1;
    }
    (mentions, scanned, "ok".to_string())
}

fn workbuddy_mentions(
    export_dir: &Path,
    cutoff: DateTime<Utc>,
) -> (HashMap<usize, usize>, u64, u64) {
    let mut mentions: HashMap<usize, usize> = HashMap::new();
    let mut files_seen = 0u64;
    let mut files_skipped = 0u64;
    if !export_dir.exists() {
        return (mentions, files_seen, files_skipped);
    }
    let extensions = [".md", ".txt", ".json", ".jsonl"];
    let paths = walk_files(export_dir, |path| {
        let ext = suffix_of(path);
        extensions.contains(&ext.as_str())
    });
    for path in paths {
        let meta = match fs::metadata(&path) {
            Ok(meta) => meta,
            Err(_) => {
                files_skipped += 1;
                continue;
            }
        };
        let modified = meta
            .modified()
            .map(|time| DateTime::<Utc>::from(time))
            .unwrap_or(DateTime::<Utc>::MIN_UTC);
        if modified < cutoff || meta.len() > MAX_SESSION_BYTES {
            continue;
        }
        let content = match read_text_file(&path) {
            Some(content) => content,
            None => {
                files_skipped += 1;
                continue;
            }
        };
        let ext = suffix_of(&path);
        let fragments: Vec<String> = if ext == ".json" {
            match serde_json::from_str::<Value>(&content) {
                Ok(value) => {
                    let mut out = Vec::new();
                    collect_fragments(&value, "", &mut out);
                    out
                }
                Err(_) => vec![content.clone()],
            }
        } else if ext == ".jsonl" {
            let mut values: Vec<Value> = Vec::new();
            let mut parsed = true;
            for line in content.lines() {
                if line.trim().is_empty() {
                    continue;
                }
                match serde_json::from_str::<Value>(line) {
                    Ok(value) => values.push(value),
                    Err(_) => {
                        parsed = false;
                        break;
                    }
                }
            }
            if parsed {
                let mut out = Vec::new();
                collect_fragments(&Value::Array(values), "", &mut out);
                out
            } else {
                vec![content.clone()]
            }
        } else {
            vec![content.clone()]
        };
        let mut found: BTreeSet<usize> = BTreeSet::new();
        for fragment in fragments {
            found.extend(technology_mentions(&fragment));
        }
        for index in found {
            *mentions.entry(index).or_insert(0) += 1;
        }
        files_seen += 1;
    }
    (mentions, files_seen, files_skipped)
}

// ---------------------------------------------------------------------------
// 公开基线
// ---------------------------------------------------------------------------

fn fetch_published_names_inner(url: &str) -> Result<HashSet<String>> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(8))
        .build()?;
    let response = client
        .get(url)
        .header("Accept", "application/json")
        .header("User-Agent", "Lyph-Tech-Stack-Review/1.0")
        .send()?;
    let mut buffer = Vec::new();
    response.take(2_000_000).read_to_end(&mut buffer)?;
    let payload: Value = serde_json::from_slice(&buffer)?;
    let payload_object = match &payload {
        Value::Object(map) => map,
        _ => return Err(anyhow!("公开站点返回的不是 JSON 对象")),
    };
    let site = payload_object.get("site").unwrap_or(&payload);
    let site_object = match site {
        Value::Object(map) => map,
        _ => return Err(anyhow!("公开站点 site 字段格式无效")),
    };
    let stack = site_object
        .get("stack")
        .and_then(|value| value.as_array())
        .cloned()
        .unwrap_or_default();
    let mut names: HashSet<String> = HashSet::new();
    for item in stack {
        if let Value::Object(map) = item {
            let raw = map
                .get("name")
                .filter(|value| python_truthy(value))
                .map(py_str)
                .or_else(|| {
                    map.get("title")
                        .filter(|value| python_truthy(value))
                        .map(py_str)
                })
                .unwrap_or_default();
            let normalized = norm(&raw);
            if !normalized.is_empty() {
                names.insert(normalized);
            }
        }
    }
    Ok(names)
}

fn fetch_published_names(url: &str) -> (HashSet<String>, String) {
    if url.trim().is_empty() {
        return (HashSet::new(), "disabled".to_string());
    }
    match fetch_published_names_inner(url) {
        Ok(names) => (names, "ok".to_string()),
        Err(_) => (HashSet::new(), "unavailable".to_string()),
    }
}

fn proficiency(project_count: usize, session_sources: usize) -> u32 {
    // 仅用于人工复核的证据估算；会话提及只占很小权重。
    (22 + project_count.min(5) as u32 * 13 + session_sources.min(3) as u32 * 2).min(95)
}

// ---------------------------------------------------------------------------
// HTML 渲染
// ---------------------------------------------------------------------------

fn render_html(report: &Value) -> String {
    let empty_object = Map::new();
    let summary = report
        .get("summary")
        .and_then(|value| value.as_object())
        .unwrap_or(&empty_object);

    let mut cards: Vec<String> = Vec::new();
    if let Some(technologies) = report.get("technologies").and_then(|value| value.as_array()) {
        for item in technologies {
            let progress = item
                .get("suggested_progress")
                .and_then(|value| value.as_i64())
                .unwrap_or(0)
                .clamp(0, 100);
            let publication = match item.get("already_published") {
                Some(Value::Bool(true)) => "公开栈已有",
                Some(Value::Bool(false)) => "建议新增候选",
                _ => "公网基线未读取",
            };
            let mut card = String::new();
            card.push_str(
                "<article class=\"card\"><div class=\"head\"><span class=\"icon\">",
            );
            card.push_str(&escape_html(
                item.get("icon").and_then(|value| value.as_str()).unwrap_or(""),
            ));
            card.push_str("</span><div><h2>");
            card.push_str(&escape_html(
                item.get("name").and_then(|value| value.as_str()).unwrap_or(""),
            ));
            card.push_str("</h2><small>");
            card.push_str(&escape_html(
                item.get("evidence_summary")
                    .and_then(|value| value.as_str())
                    .unwrap_or(""),
            ));
            card.push_str("</small></div></div><p>");
            card.push_str(&escape_html(
                item.get("description")
                    .and_then(|value| value.as_str())
                    .unwrap_or(""),
            ));
            card.push_str("</p><p class=\"how\"><b>如何使用：</b>");
            card.push_str(&escape_html(
                item.get("how").and_then(|value| value.as_str()).unwrap_or(""),
            ));
            card.push_str("</p><div class=\"meter\"><span style=\"width:");
            card.push_str(&progress.to_string());
            card.push_str("%\"></span></div><div class=\"foot\">实作证据估算 · ");
            card.push_str(&progress.to_string());
            card.push_str("% · ");
            card.push_str(publication);
            card.push_str(" · 待人工复核</div></article>");
            cards.push(card);
        }
    }
    let cards = if cards.is_empty() {
        "<p class=\"empty\">本周没有发现可供复核的新技术条目。采集摘要仍可在 JSON 报告中查看。</p>"
            .to_string()
    } else {
        cards.join("\n")
    };

    let generated = escape_html(
        report
            .get("generated_at")
            .and_then(|value| value.as_str())
            .unwrap_or(""),
    );
    let projects = summary.get("projects_scanned").map(py_str).unwrap_or_else(|| "0".to_string());
    let baseline = escape_html(
        summary
            .get("public_baseline_status")
            .and_then(|value| value.as_str())
            .unwrap_or(""),
    );
    let codex_files = summary.get("codex_files_scanned").map(py_str).unwrap_or_else(|| "0".to_string());
    let opencode_messages = summary
        .get("opencode_messages_scanned")
        .map(py_str)
        .unwrap_or_else(|| "0".to_string());
    let workbuddy_files = summary
        .get("workbuddy_files_scanned")
        .map(py_str)
        .unwrap_or_else(|| "0".to_string());

    let mut html = String::new();
    html.push_str("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n<title>Lyph 技术栈周更草案</title><style>\n");
    html.push_str("*{box-sizing:border-box}body{margin:0;background:#f5f3ee;color:#20211f;font:15px/1.65 system-ui,\"Microsoft YaHei\",sans-serif}main{max-width:1040px;margin:0 auto;padding:40px 22px 64px}h1{font-size:32px;letter-spacing:-.03em;margin:8px 0}.eyebrow,small{color:#6f706d}.notice{border:1px solid #d7d3ca;background:#fffdf8;border-radius:14px;padding:16px 18px;margin:20px 0 26px}.notice strong{color:#954b2e}.summary{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 28px}.pill{background:#e9e6df;border-radius:999px;padding:5px 12px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px}.card{background:#fff;border:1px solid #e4e1da;border-radius:16px;padding:18px;box-shadow:0 5px 22px #1e1c1609}.head{display:flex;align-items:center;gap:12px}.icon{font-size:27px}.card h2{font-size:19px;margin:0}.card p{margin:12px 0;color:#434541}.how{font-size:14px}.meter{height:8px;border-radius:8px;background:#e9e7e2;overflow:hidden;margin-top:17px}.meter span{display:block;height:100%;background:#628d76;border-radius:inherit}.foot{font-size:12px;color:#777;margin-top:5px}.empty{background:white;padding:20px;border-radius:12px}.meta{font-size:13px;color:#676863}code{background:#ebe9e4;padding:2px 5px;border-radius:4px}@media(max-width:600px){main{padding:26px 14px}h1{font-size:27px}}\n");
    html.push_str("</style><main><span class=\"eyebrow\">LOCAL REVIEW · 每周自动采集草案</span><h1>Lyph 技术栈周更</h1><div class=\"meta\">生成时间：");
    html.push_str(&generated);
    html.push_str("</div>\n<div class=\"notice\"><strong>待人工复核，未发布。</strong> 本报告只展示技术名称、图标、用途、使用方式和基于实作证据的进度估算；不会包含项目路径、会话标题或对话原文，也不会自动改写公网资料。</div>\n<div class=\"summary\"><span class=\"pill\">项目：");
    html.push_str(&projects);
    html.push_str(" 个</span><span class=\"pill\">公开栈匹配：");
    html.push_str(&baseline);
    html.push_str("</span><span class=\"pill\">Codex 会话：");
    html.push_str(&codex_files);
    html.push_str("</span><span class=\"pill\">OpenCode 消息：");
    html.push_str(&opencode_messages);
    html.push_str("</span><span class=\"pill\">WorkBuddy 导出：");
    html.push_str(&workbuddy_files);
    html.push_str("</span></div>\n<div class=\"grid\">");
    html.push_str(&cards);
    html.push_str("</div><p class=\"meta\" style=\"margin-top:28px\">WorkBuddy：将官方历史导出文件放进桌面 Lyph 技术栈周更/workbuddy-exports，下次计划任务会读取。公网更新仍在站主后台确认后进行。</p></main></html>");
    html
}

// ---------------------------------------------------------------------------
// 报告构建与落盘
// ---------------------------------------------------------------------------

fn clamp_history(config: &Value) -> i64 {
    let raw = config
        .get("history_days")
        .and_then(json_to_i64)
        .unwrap_or(365);
    raw.clamp(30, 730)
}

fn config_string(config: &Value, key: &str, fallback: &Path) -> PathBuf {
    config
        .get(key)
        .and_then(|value| value.as_str())
        .filter(|text| !text.trim().is_empty())
        .map(expand_path)
        .unwrap_or_else(|| fallback.to_path_buf())
}

fn sort_technologies(items: &mut [Value]) {
    items.sort_by(|left, right| {
        let left_count = left
            .get("project_count")
            .and_then(|value| value.as_i64())
            .unwrap_or(0);
        let right_count = right
            .get("project_count")
            .and_then(|value| value.as_i64())
            .unwrap_or(0);
        right_count.cmp(&left_count).then_with(|| {
            let left_name = left
                .get("name")
                .and_then(|value| value.as_str())
                .unwrap_or("")
                .to_lowercase();
            let right_name = right
                .get("name")
                .and_then(|value| value.as_str())
                .unwrap_or("")
                .to_lowercase();
            left_name.cmp(&right_name)
        })
    });
}

fn build_report(
    config: &Value,
    now: DateTime<Utc>,
    default_export_dir: &Path,
) -> Result<Value> {
    let history_days = clamp_history(config);
    let cutoff = now - ChronoDuration::days(history_days);
    let max_depth = config
        .get("max_depth")
        .and_then(json_to_i64)
        .unwrap_or(7);

    let roots: Vec<PathBuf> = config
        .get("project_roots")
        .and_then(|value| value.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|value| value.as_str())
                .map(expand_path)
                .collect()
        })
        .unwrap_or_default();
    let projects = discover_projects(&roots, max_depth);
    let mut by_project: HashMap<usize, BTreeSet<String>> = HashMap::new();
    for (path, evidence) in &projects {
        let key = path.to_string_lossy().to_string();
        for index in evidence {
            by_project.entry(*index).or_default().insert(key.clone());
        }
    }

    let home = home_dir();
    let codex_home = config_string(config, "codex_home", &home.join(".codex"));
    let (codex, codex_files, codex_skipped) = codex_mentions(&codex_home, cutoff);

    let opencode_default = home
        .join(".local")
        .join("share")
        .join("opencode")
        .join("opencode.db");
    let opencode_path = config_string(config, "opencode_database", &opencode_default);
    let (opencode, opencode_messages, opencode_status) =
        opencode_mentions(&opencode_path, cutoff);

    let export_dir = config_string(config, "workbuddy_exports", default_export_dir);
    let (workbuddy, workbuddy_files, workbuddy_skipped) =
        workbuddy_mentions(&export_dir, cutoff);

    let total = CATALOG.len();
    let mut session_counts = vec![0usize; total];
    let mut session_sources = vec![0usize; total];
    for index in 0..total {
        let codex_hits = codex.get(&index).copied().unwrap_or(0);
        let opencode_hits = opencode.get(&index).copied().unwrap_or(0);
        let workbuddy_hits = workbuddy.get(&index).copied().unwrap_or(0);
        session_counts[index] = codex_hits + opencode_hits + workbuddy_hits;
        session_sources[index] = [codex_hits, opencode_hits, workbuddy_hits]
            .iter()
            .filter(|hits| **hits > 0)
            .count();
    }

    let public_url = config
        .get("public_site_url")
        .and_then(|value| value.as_str())
        .unwrap_or("");
    let (published, baseline_status) = fetch_published_names(public_url);

    let mut technologies: Vec<Value> = Vec::new();
    for (index, row) in CATALOG.iter().enumerate() {
        let project_count = by_project.get(&index).map(|paths| paths.len()).unwrap_or(0);
        if project_count == 0 {
            continue;
        }
        let mentions = session_counts[index];
        let mut sources: Vec<&str> = Vec::new();
        if codex.get(&index).copied().unwrap_or(0) > 0 {
            sources.push("Codex");
        }
        if opencode.get(&index).copied().unwrap_or(0) > 0 {
            sources.push("OpenCode");
        }
        if workbuddy.get(&index).copied().unwrap_or(0) > 0 {
            sources.push("WorkBuddy");
        }
        let already_published = if baseline_status == "ok" {
            let mut keys: HashSet<String> = HashSet::new();
            keys.insert(norm(row.0));
            for alias in row.4 {
                keys.insert(norm(alias));
            }
            for package in row.5 {
                keys.insert(norm(package));
            }
            Value::Bool(keys.iter().any(|key| published.contains(key)))
        } else {
            Value::Null
        };
        technologies.push(json!({
            "name": row.0,
            "icon": row.1,
            "description": row.2,
            "how": row.3,
            "suggested_progress": proficiency(project_count, session_sources[index]),
            "project_count": project_count,
            "agent_text_units_with_mention": mentions,
            "agent_sources": sources,
            "evidence_summary": format!("{project_count} 个工程目录 · {mentions} 个 Agent 文本单元命中技术名"),
            "already_published": already_published,
        }));
    }
    sort_technologies(&mut technologies);

    let mut observations: Vec<Value> = Vec::new();
    for (index, row) in CATALOG.iter().enumerate() {
        if session_counts[index] == 0 || by_project.contains_key(&index) {
            continue;
        }
        let mut sources: Vec<&str> = Vec::new();
        if codex.get(&index).copied().unwrap_or(0) > 0 {
            sources.push("Codex");
        }
        if opencode.get(&index).copied().unwrap_or(0) > 0 {
            sources.push("OpenCode");
        }
        if workbuddy.get(&index).copied().unwrap_or(0) > 0 {
            sources.push("WorkBuddy");
        }
        observations.push(json!({
            "name": row.0,
            "icon": row.1,
            "mentions": session_counts[index],
            "sources": sources,
        }));
    }

    let generated_at = now
        .with_timezone(&Local)
        .format("%Y-%m-%dT%H:%M:%S%:z")
        .to_string();

    Ok(json!({
        "schema_version": 1,
        "generated_at": generated_at,
        "status": "draft_only",
        "summary": {
            "projects_scanned": projects.len(),
            "codex_files_scanned": codex_files,
            "codex_files_skipped": codex_skipped,
            "opencode_messages_scanned": opencode_messages,
            "opencode_status": opencode_status,
            "workbuddy_files_scanned": workbuddy_files,
            "workbuddy_files_skipped": workbuddy_skipped,
            "public_baseline_status": baseline_status,
            "history_days": history_days,
            "raw_transcripts_uploaded": false,
        },
        "technologies": technologies,
        "session_only_observations": observations,
    }))
}

fn prune_reports(output_dir: &Path) {
    let mut files: Vec<(SystemTime, PathBuf)> = Vec::new();
    if let Ok(entries) = fs::read_dir(output_dir) {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if !name.starts_with("tech-stack-weekly-") || !name.ends_with(".json") {
                continue;
            }
            let path = entry.path();
            let modified = match fs::metadata(&path).and_then(|meta| meta.modified()) {
                Ok(modified) => modified,
                Err(_) => continue,
            };
            files.push((modified, path));
        }
    }
    files.sort_by(|left, right| right.0.cmp(&left.0));
    for (_, path) in files.into_iter().skip(MAX_KEEP_REPORTS) {
        let _ = fs::remove_file(&path);
        let _ = fs::remove_file(path.with_extension("html"));
    }
}

fn collect_and_write(
    config: &Value,
    default_report_dir: &Path,
    default_export_dir: &Path,
) -> Result<(PathBuf, PathBuf)> {
    let now = Utc::now();
    let report = build_report(config, now, default_export_dir)?;
    let output_dir = config
        .get("output_dir")
        .and_then(|value| value.as_str())
        .filter(|text| !text.trim().is_empty())
        .map(expand_path)
        .unwrap_or_else(|| default_report_dir.to_path_buf());
    fs::create_dir_all(&output_dir)?;

    let serialized = serde_json::to_string_pretty(&report)?;
    let html = render_html(&report);
    let stamp = now.format("%Y%m%d-%H%M%S").to_string();
    let json_path = output_dir.join(format!("tech-stack-weekly-{stamp}.json"));
    let html_path = output_dir.join(format!("tech-stack-weekly-{stamp}.html"));
    let latest_json = output_dir.join("latest.json");
    let latest_html = output_dir.join("latest.html");

    fs::write(&json_path, serialized.as_bytes())?;
    fs::write(&html_path, html.as_bytes())?;
    fs::write(&latest_json, serialized.as_bytes())?;
    fs::write(&latest_html, html.as_bytes())?;
    prune_reports(&output_dir);
    Ok((latest_json, latest_html))
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

fn detect_project_roots() -> Vec<String> {
    let home = home_dir();
    let candidates = [
        PathBuf::from("D:/xx"),
        PathBuf::from("D:/工作"),
        PathBuf::from("D:/论文"),
        PathBuf::from("D:/期末作业"),
        home.join("PycharmProjects"),
        home.join("CodeBuddy"),
        home.join("WorkBuddy"),
        home.join("wechat-agent-workspace"),
    ];
    let mut roots: Vec<String> = Vec::new();
    for candidate in candidates {
        if candidate.is_dir() {
            let text = candidate.to_string_lossy().to_string();
            if !roots.contains(&text) {
                roots.push(text);
            }
        }
    }
    if roots.is_empty() {
        roots.push(home.to_string_lossy().to_string());
    }
    roots
}

fn default_config(report_dir: &Path, export_dir: &Path) -> Map<String, Value> {
    let home = home_dir();
    let mut config = Map::new();
    config.insert("enabled".to_string(), json!(true));
    config.insert("weekly_day".to_string(), json!(6));
    config.insert("weekly_time".to_string(), json!("10:00"));
    config.insert("project_roots".to_string(), json!(detect_project_roots()));
    config.insert(
        "output_dir".to_string(),
        json!(report_dir.to_string_lossy()),
    );
    config.insert(
        "workbuddy_exports".to_string(),
        json!(export_dir.to_string_lossy()),
    );
    config.insert(
        "codex_home".to_string(),
        json!(home.join(".codex").to_string_lossy()),
    );
    config.insert(
        "opencode_database".to_string(),
        json!(home
            .join(".local")
            .join("share")
            .join("opencode")
            .join("opencode.db")
            .to_string_lossy()),
    );
    config.insert(
        "public_site_url".to_string(),
        json!("https://123.57.153.49/api/v1/public/site"),
    );
    config.insert("history_days".to_string(), json!(365));
    config.insert("max_depth".to_string(), json!(7));
    config
}

fn write_config_atomic(path: &Path, config: &Value) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let temporary = path.with_extension("json.tmp");
    let serialized = serde_json::to_string_pretty(config)?;
    fs::write(&temporary, serialized.as_bytes())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600));
    }
    fs::rename(&temporary, path)?;
    Ok(())
}

fn load_or_create_config(
    config_path: &Path,
    report_dir: &Path,
    export_dir: &Path,
) -> Map<String, Value> {
    let defaults = default_config(report_dir, export_dir);
    let mut data: Option<Map<String, Value>> = fs::read_to_string(config_path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(strip_bom(&text)).ok())
        .and_then(|value| match value {
            Value::Object(map) => Some(map),
            _ => None,
        });

    match data.as_mut() {
        Some(map) => {
            let mut changed = false;
            for (key, value) in &defaults {
                if !map.contains_key(key) {
                    map.insert(key.clone(), value.clone());
                    changed = true;
                }
            }
            // 线上曾经出现过空 project_roots，导致完全扫不到工程；这里回填默认探测结果。
            let roots_empty = map
                .get("project_roots")
                .and_then(|value| value.as_array())
                .map(|items| items.is_empty())
                .unwrap_or(true);
            if roots_empty {
                map.insert("project_roots".to_string(), json!(detect_project_roots()));
                changed = true;
            }
            if changed {
                let _ = write_config_atomic(config_path, &Value::Object(map.clone()));
            }
            map.clone()
        }
        None => {
            let _ = write_config_atomic(config_path, &Value::Object(defaults.clone()));
            defaults
        }
    }
}

fn parse_weekly_time(value: &str) -> Result<(u32, u32)> {
    let (hour, minute) = value
        .split_once(':')
        .ok_or_else(|| anyhow!("每周时间格式应为 HH:MM"))?;
    let hour: u32 = hour
        .trim()
        .parse()
        .map_err(|_| anyhow!("每周时间格式应为 HH:MM"))?;
    let minute: u32 = minute
        .trim()
        .parse()
        .map_err(|_| anyhow!("每周时间格式应为 HH:MM"))?;
    if hour > 23 || minute > 59 {
        return Err(anyhow!("每周时间无效"));
    }
    Ok((hour, minute))
}

fn classify_error(error: &anyhow::Error) -> String {
    if error.downcast_ref::<std::io::Error>().is_some() {
        return "OSError".to_string();
    }
    if let Some(inner) = error.downcast_ref::<serde_json::Error>() {
        return if inner.is_data() {
            "JSONDecodeError".to_string()
        } else {
            "TypeError".to_string()
        };
    }
    if error.downcast_ref::<toml::de::Error>().is_some() {
        return "TOMLDecodeError".to_string();
    }
    let text = error.to_string();
    if text.contains("无效") || text.contains("应为") || text.contains("必须") {
        return "ValueError".to_string();
    }
    "RuntimeError".to_string()
}

fn open_in_shell(path: &Path) {
    #[cfg(target_os = "windows")]
    {
        let _ = std::process::Command::new("explorer").arg(path).spawn();
    }
    #[cfg(target_os = "macos")]
    {
        let _ = std::process::Command::new("open").arg(path).spawn();
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let _ = std::process::Command::new("xdg-open").arg(path).spawn();
    }
}

// ---------------------------------------------------------------------------
// 服务
// ---------------------------------------------------------------------------

struct Inner {
    report_dir: PathBuf,
    export_dir: PathBuf,
    config_path: PathBuf,
    log: Arc<EventLog>,
    config: Mutex<Map<String, Value>>,
    running: AtomicBool,
    last_error: Mutex<String>,
    stopped: Mutex<bool>,
    wake: Condvar,
    scheduler: Mutex<Option<JoinHandle<()>>>,
}

fn lock_or_recover<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl Inner {
    fn config_value(&self) -> Value {
        Value::Object(lock_or_recover(&self.config).clone())
    }

    fn config_get(&self, key: &str) -> Option<Value> {
        lock_or_recover(&self.config).get(key).cloned()
    }

    fn latest_report(&self) -> Value {
        let path = self.report_dir.join("latest.json");
        match fs::read_to_string(&path) {
            Ok(text) => match serde_json::from_str::<Value>(strip_bom(&text)) {
                Ok(value) if value.is_object() => value,
                _ => json!({}),
            },
            Err(_) => json!({}),
        }
    }

    fn last_success(&self) -> Option<DateTime<Local>> {
        let report = self.latest_report();
        let value = report
            .get("generated_at")
            .and_then(|item| item.as_str())
            .unwrap_or("");
        if value.is_empty() {
            return None;
        }
        if let Ok(parsed) = DateTime::parse_from_rfc3339(value) {
            return Some(parsed.with_timezone(&Local));
        }
        for format in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S"] {
            if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(value, format) {
                return Local.from_local_datetime(&naive).earliest();
            }
        }
        None
    }

    fn next_scheduled(&self, now: DateTime<Local>) -> DateTime<Local> {
        let weekly_time = lock_or_recover(&self.config)
            .get("weekly_time")
            .and_then(|value| value.as_str())
            .filter(|text| !text.is_empty())
            .unwrap_or("10:00")
            .to_string();
        let (hour, minute) = parse_weekly_time(&weekly_time).unwrap_or((10, 0));
        let weekly_day = lock_or_recover(&self.config)
            .get("weekly_day")
            .and_then(json_to_i64)
            .unwrap_or(6)
            .clamp(0, 6);

        let today_weekday = now.weekday().num_days_from_monday() as i64;
        let days = (weekly_day - today_weekday).rem_euclid(7);
        let time = NaiveTime::from_hms_opt(hour, minute, 0).unwrap_or_else(|| {
            NaiveTime::from_hms_opt(10, 0, 0).expect("10:00 一定合法")
        });
        let today = now.date_naive().and_time(time);
        let mut candidate = Local
            .from_local_datetime(&today)
            .earliest()
            .unwrap_or(now)
            + ChronoDuration::days(days);
        let last = self.last_success();
        if let Some(last) = last {
            while candidate <= last {
                candidate += ChronoDuration::days(7);
            }
        }
        if candidate <= now {
            now
        } else {
            candidate
        }
    }

    fn status(&self) -> Value {
        let report = self.latest_report();
        let summary = report
            .get("summary")
            .filter(|value| value.is_object())
            .cloned()
            .unwrap_or_else(|| json!({}));
        let running = self.running.load(Ordering::SeqCst);
        let last_error = lock_or_recover(&self.last_error).clone();
        let config = self.config_value();
        let enabled = config
            .get("enabled")
            .map(python_truthy)
            .unwrap_or(true);
        let weekly_day = config
            .get("weekly_day")
            .and_then(json_to_i64)
            .unwrap_or(6);
        let weekly_time = config
            .get("weekly_time")
            .and_then(|value| value.as_str())
            .filter(|text| !text.is_empty())
            .unwrap_or("10:00")
            .to_string();
        let next_run_at = if enabled {
            let next = match self.last_success() {
                None => Local::now(),
                Some(_) => self.next_scheduled(Local::now()),
            };
            Value::String(next.format("%Y-%m-%dT%H:%M%:z").to_string())
        } else {
            Value::Null
        };
        let technology_count = report
            .get("technologies")
            .and_then(|value| value.as_array())
            .map(|items| items.len())
            .unwrap_or(0);
        let project_roots = config
            .get("project_roots")
            .cloned()
            .unwrap_or_else(|| json!([]));
        let last_completed_at = report
            .get("generated_at")
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .to_string();
        let public_baseline_status = summary
            .get("public_baseline_status")
            .and_then(|value| value.as_str())
            .filter(|text| !text.is_empty())
            .unwrap_or("尚未采集")
            .to_string();

        json!({
            "running": running,
            "enabled": enabled,
            "weekly_day": weekly_day,
            "weekly_time": weekly_time,
            "next_run_at": next_run_at,
            "last_completed_at": last_completed_at,
            "last_error": last_error,
            "technology_count": technology_count,
            "summary": {
                "projects_scanned": summary.get("projects_scanned").cloned().unwrap_or_else(|| json!(0)),
                "codex_files_scanned": summary.get("codex_files_scanned").cloned().unwrap_or_else(|| json!(0)),
                "opencode_messages_scanned": summary.get("opencode_messages_scanned").cloned().unwrap_or_else(|| json!(0)),
                "workbuddy_files_scanned": summary.get("workbuddy_files_scanned").cloned().unwrap_or_else(|| json!(0)),
            },
            "paths": {
                "report": self.report_dir.join("latest.html").to_string_lossy(),
                "reports": self.report_dir.to_string_lossy(),
                "exports": self.export_dir.to_string_lossy(),
                "config": self.config_path.to_string_lossy(),
            },
            "config": config,
            "report": report,
            "project_roots": project_roots,
            "public_baseline_status": public_baseline_status,
        })
    }

    fn collect_body(&self) -> Result<()> {
        let config = self.config_value();
        collect_and_write(&config, &self.report_dir, &self.export_dir)?;
        Ok(())
    }

    fn record_error_text(&self, kind: &str) {
        *lock_or_recover(&self.last_error) = kind.to_string();
        self.log
            .add("ERROR", &format!("技术栈周更采集失败：{kind}"));
    }

    fn record_error(&self, error: &anyhow::Error) {
        let kind = classify_error(error);
        self.record_error_text(&kind);
    }

    fn collect_guarded(&self) -> bool {
        if self.running.swap(true, Ordering::SeqCst) {
            return false;
        }
        *lock_or_recover(&self.last_error) = String::new();
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.collect_body()));
        let success = match outcome {
            Ok(Ok(())) => {
                self.log
                    .add("INFO", "技术栈周更采集完成，报告保存在本机");
                true
            }
            Ok(Err(error)) => {
                self.record_error(&error);
                false
            }
            Err(_) => {
                self.record_error_text("PanicError");
                false
            }
        };
        self.running.store(false, Ordering::SeqCst);
        success
    }

    fn is_stopped(&self) -> bool {
        *lock_or_recover(&self.stopped)
    }

    fn wait_interruptible(&self, seconds: u64) {
        let guard = lock_or_recover(&self.stopped);
        if *guard {
            return;
        }
        let _ = self
            .wake
            .wait_timeout(guard, Duration::from_secs(seconds.max(1)));
    }
}

fn schedule_loop(inner: Arc<Inner>) {
    loop {
        if inner.is_stopped() {
            return;
        }
        let enabled = inner
            .config_get("enabled")
            .map(|value| python_truthy(&value))
            .unwrap_or(true);
        if !enabled {
            inner.wait_interruptible(60);
            continue;
        }
        let now = Local::now();
        let due = match inner.last_success() {
            None => now,
            Some(_) => inner.next_scheduled(now),
        };
        if due <= now {
            if inner.running.load(Ordering::SeqCst) {
                // 手工采集正在运行：稍后再判断，避免把这一轮当成失败而等一小时。
                inner.wait_interruptible(60);
                continue;
            }
            let success = inner.collect_guarded();
            if !success {
                inner.wait_interruptible(3600);
            }
            continue;
        }
        let seconds = (due - now).num_seconds().clamp(1, 60) as u64;
        inner.wait_interruptible(seconds);
    }
}

/// 本机技术栈周更服务。
pub struct TechStackService {
    inner: Arc<Inner>,
}

impl TechStackService {
    /// `root` 是数据目录，会自动创建 `reports/` 与 `workbuddy-exports/`。
    pub fn new(root: PathBuf, log: Arc<EventLog>) -> Self {
        let report_dir = root.join("reports");
        let export_dir = root.join("workbuddy-exports");
        let _ = fs::create_dir_all(&root);
        let _ = fs::create_dir_all(&report_dir);
        let _ = fs::create_dir_all(&export_dir);
        let config_path = root.join("config.json");
        let config = load_or_create_config(&config_path, &report_dir, &export_dir);
        Self {
            inner: Arc::new(Inner {
                report_dir,
                export_dir,
                config_path,
                log,
                config: Mutex::new(config),
                running: AtomicBool::new(false),
                last_error: Mutex::new(String::new()),
                stopped: Mutex::new(false),
                wake: Condvar::new(),
                scheduler: Mutex::new(None),
            }),
        }
    }

    /// 启动每周调度线程（幂等）。
    pub fn start(&self) {
        let mut guard = lock_or_recover(&self.inner.scheduler);
        if let Some(handle) = guard.as_ref() {
            if !handle.is_finished() {
                return;
            }
        }
        *lock_or_recover(&self.inner.stopped) = false;
        self.inner.wake.notify_all();
        let inner = self.inner.clone();
        *guard = Some(std::thread::spawn(move || schedule_loop(inner)));
    }

    /// 停止调度线程（幂等）。采集本身不可中断，等价于 Python 的 2 秒超时后放弃 join。
    pub fn stop(&self) {
        let handle = lock_or_recover(&self.inner.scheduler).take();
        *lock_or_recover(&self.inner.stopped) = true;
        self.inner.wake.notify_all();
        if let Some(handle) = handle {
            if handle.is_finished() {
                let _ = handle.join();
            }
        }
    }

    /// 与现有 UI 兼容的状态 JSON。
    pub fn status(&self) -> Value {
        self.inner.status()
    }

    /// 立即采集：后台线程执行，立即返回当前状态（running=true）。
    pub fn run_now(&self) -> Value {
        let started = self
            .inner
            .running
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok();
        // 先取快照再启动线程，保证返回的 running 一定为 true。
        let status = self.status();
        if started {
            let inner = self.inner.clone();
            std::thread::spawn(move || {
                *lock_or_recover(&inner.last_error) = String::new();
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    inner.collect_body()
                }));
                match outcome {
                    Ok(Ok(())) => {
                        inner
                            .log
                            .add("INFO", "技术栈周更采集完成，报告保存在本机");
                    }
                    Ok(Err(error)) => inner.record_error(&error),
                    Err(_) => inner.record_error_text("PanicError"),
                }
                inner.running.store(false, Ordering::SeqCst);
            });
        }
        status
    }

    /// 保存计划配置，只接受 enabled / weekly_day / weekly_time。
    pub fn update_config(&self, payload: &Value) -> Result<Value> {
        let mut config = lock_or_recover(&self.inner.config).clone();
        let object = payload
            .as_object()
            .ok_or_else(|| anyhow!("配置必须是 JSON 对象"))?;
        if let Some(value) = object.get("enabled") {
            config.insert("enabled".to_string(), Value::Bool(python_truthy(value)));
        }
        if let Some(value) = object.get("weekly_day") {
            let day = json_to_i64(value).ok_or_else(|| anyhow!("星期格式无效"))?;
            if !(0..=6).contains(&day) {
                return Err(anyhow!("星期范围应为 0 到 6"));
            }
            config.insert("weekly_day".to_string(), json!(day));
        }
        if let Some(value) = object.get("weekly_time") {
            let text = py_str_or_empty(value);
            parse_weekly_time(&text)?;
            config.insert("weekly_time".to_string(), Value::String(text));
        }
        write_config_atomic(&self.inner.config_path, &Value::Object(config.clone()))?;
        *lock_or_recover(&self.inner.config) = config;
        self.inner.log.add("INFO", "技术栈周更计划已保存");
        Ok(self.status())
    }

    /// 打开报告/目录/配置：kind ∈ {"report","reports","exports","config"}。
    pub fn open_path(&self, kind: &str) -> Result<()> {
        let target = match kind {
            "report" => self.inner.report_dir.join("latest.html"),
            "reports" => self.inner.report_dir.clone(),
            "exports" => self.inner.export_dir.clone(),
            "config" => self.inner.config_path.clone(),
            _ => return Err(anyhow!("未知的技术栈路径")),
        };
        if kind == "report" && !target.is_file() {
            return Err(anyhow!("还没有技术栈报告，请先立即采集"));
        }
        if kind == "config" && !target.is_file() {
            return Err(anyhow!("技术栈配置不存在"));
        }
        open_in_shell(&target);
        Ok(())
    }
}

impl Drop for TechStackService {
    fn drop(&mut self) {
        self.stop();
    }
}

// ---------------------------------------------------------------------------
// 自测
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    static TEMP_SEQ: AtomicUsize = AtomicUsize::new(0);

    fn temp_dir(label: &str) -> PathBuf {
        let seq = TEMP_SEQ.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!(
            "wechat-agent-tech-stack-{label}-{}-{seq}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("创建临时目录");
        dir
    }

    #[test]
    fn norm_removes_non_alphanumeric() {
        assert_eq!(norm("Next.js"), "nextjs");
        assert_eq!(norm("Vue 3"), "vue3");
        assert_eq!(norm("C++"), "c");
        assert_eq!(norm("微信小程序"), "");
    }

    #[test]
    fn package_name_handles_specs_and_scopes() {
        assert_eq!(package_name("Django>=4.2"), "django");
        assert_eq!(package_name("  React "), "react");
        assert_eq!(package_name("@scope/pkg"), "pkg");
        assert_eq!(package_name("Flask[async]"), "flask");
        assert_eq!(package_name("requests; python_version<'3.11'"), "requests");
        assert_eq!(package_name("org/springframework/boot"), "boot");
    }

    #[test]
    fn proficiency_matches_python_formula() {
        assert_eq!(proficiency(0, 0), 22);
        assert_eq!(proficiency(1, 1), 37);
        assert_eq!(proficiency(3, 2), 65);
        assert_eq!(proficiency(5, 3), 93);
        assert_eq!(proficiency(30, 30), 93);
    }

    #[test]
    fn catalog_contains_expected_entries() {
        assert_eq!(CATALOG.len(), 56);
        assert_eq!(catalog_index("React"), Some(2));
        assert_eq!(catalog_index("WorkBuddy"), Some(55));
        // 包名索引应把依赖规范名归一化后映射回技术。
        let by_package = &catalog_indexes().by_package;
        assert_eq!(by_package.get("fastapi").copied(), catalog_index("FastAPI"));
        assert_eq!(by_package.get("next").copied(), catalog_index("Next.js"));
    }

    #[test]
    fn project_evidence_detects_manifest_and_source_suffix() {
        let dir = temp_dir("evidence");
        fs::create_dir_all(dir.join("src")).expect("创建 src");
        fs::write(
            dir.join("package.json"),
            r#"{"name":"demo","scripts":{"dev":"vite"},"dependencies":{"react":"^18.2.0"}}"#,
        )
        .expect("写入 package.json");
        fs::write(dir.join("src/main.py"), "print('hi')\n").expect("写入 main.py");

        let evidence = project_evidence(&dir);
        assert!(evidence.contains(&catalog_index("React").unwrap()));
        assert!(evidence.contains(&catalog_index("JavaScript").unwrap()));
        assert!(evidence.contains(&catalog_index("Python").unwrap()));

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn discovered_projects_require_manifest() {
        let root = temp_dir("discover");
        let project = root.join("app");
        fs::create_dir_all(project.join("src")).expect("创建工程目录");
        fs::write(project.join("Cargo.toml"), "[package]\nname=\"demo\"\n")
            .expect("写入 Cargo.toml");
        fs::write(project.join("src/main.rs"), "fn main() {}\n").expect("写入 main.rs");
        fs::create_dir_all(root.join("empty")).expect("创建空目录");

        let projects = discover_projects(&[root.clone()], 7);
        assert_eq!(projects.len(), 1);
        let evidence = projects.values().next().unwrap();
        assert!(evidence.contains(&catalog_index("Rust").unwrap()));

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn technologies_sort_by_count_then_name() {
        let mut items = vec![
            json!({"name": "beta", "project_count": 1}),
            json!({"name": "Alpha", "project_count": 3}),
            json!({"name": "zeta", "project_count": 3}),
        ];
        sort_technologies(&mut items);
        assert_eq!(items[0]["name"], "Alpha");
        assert_eq!(items[1]["name"], "zeta");
        assert_eq!(items[2]["name"], "beta");
    }

    #[test]
    fn html_escape_and_empty_state() {
        assert_eq!(escape_html("a<b>&\"'"), "a&lt;b&gt;&amp;&quot;&#x27;");
        let report = json!({
            "generated_at": "2026-11-10T10:00:00+08:00",
            "summary": {"projects_scanned": 0, "public_baseline_status": "尚未采集", "codex_files_scanned": 0, "opencode_messages_scanned": 0, "workbuddy_files_scanned": 0},
            "technologies": [],
        });
        let html = render_html(&report);
        assert!(html.contains("本周没有发现可供复核的新技术条目"));
        assert!(html.contains("生成时间：2026-11-10T10:00:00+08:00"));
    }

    #[test]
    fn secret_fragments_never_produce_mentions() {
        assert!(technology_mentions("请把 api_key 放到服务端配置里").is_empty());
        assert!(!technology_mentions("我们用 React 写页面").is_empty());
    }
}
