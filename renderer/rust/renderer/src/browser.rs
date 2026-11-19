use std::collections::HashSet;
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail, ensure};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tempfile::TempDir;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;
use tokio::process::{Child, Command};
use tokio::time::{Instant, sleep_until, timeout};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, connect_async};

use crate::payload::origin;

const NETWORK_IDLE: Duration = Duration::from_millis(500);

// Chromium defaults from Playwright 1.49.1, followed by the sidecar's arguments.
const CHROMIUM_ARGS: &[&str] = &[
    "--disable-field-trial-config",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-back-forward-cache",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages",
    "--disable-component-update",
    "--no-default-browser-check",
    "--disable-default-apps",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-features=ImprovedCookieControls,LazyFrameLoading,GlobalMediaControls,DestroyProfileOnBrowserClose,MediaRouter,DialMediaRouteProvider,AcceptCHFrame,AutoExpandDetailsElement,CertificateTransparencyComponentUpdater,AvoidUnnecessaryBeforeUnloadCheckSync,Translate,HttpsUpgrades,PaintHolding,ThirdPartyStoragePartitioning,LensOverlay,PlzDedicatedWorker",
    "--allow-pre-commit-input",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--force-color-profile=srgb",
    "--metrics-recording-only",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    "--no-service-autorun",
    "--export-tagged-pdf",
    "--disable-search-engine-choice-screen",
    "--unsafely-disable-devtools-self-xss-warnings",
    "--enable-automation",
    "--headless",
    "--hide-scrollbars",
    "--mute-audio",
    "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--js-flags=--max-old-space-size=128",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
    "--no-startup-window",
];

pub(crate) struct Browser {
    _child: Child,
    _profile: TempDir,
    endpoint: String,
}

impl Browser {
    pub(crate) async fn launch() -> Result<Self> {
        let executable = browser_executable()?;
        let profile = tempfile::tempdir()?;
        let mut profile_arg = OsString::from("--user-data-dir=");
        profile_arg.push(profile.path());
        let mut child = Command::new(executable)
            .args(CHROMIUM_ARGS)
            .arg(profile_arg)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .context("launching Chromium headless shell")?;
        let stderr = child.stderr.take().context("missing Chromium stderr")?;
        let mut lines = BufReader::new(stderr).lines();
        let endpoint = timeout(Duration::from_secs(30), async {
            while let Some(line) = lines.next_line().await? {
                if let Some(endpoint) = line.strip_prefix("DevTools listening on ws://127.0.0.1:") {
                    return Ok(format!("ws://127.0.0.1:{endpoint}"));
                }
            }
            bail!("Chromium exited before opening DevTools")
        })
        .await
        .context("timed out starting Chromium")??;
        tokio::spawn(async move { while matches!(lines.next_line().await, Ok(Some(_))) {} });
        Ok(Self {
            _child: child,
            _profile: profile,
            endpoint,
        })
    }

    pub(crate) async fn open<'a>(&self, allowed: &'a [String]) -> Result<Page<'a>> {
        let (socket, _) = connect_async(&self.endpoint).await?;
        let mut page = Page {
            socket,
            next_id: 0,
            allowed,
            context: String::new(),
            session: String::new(),
            navigation: Navigation::default(),
        };
        let context = page
            .send(
                None,
                "Target.createBrowserContext",
                json!({"disposeOnDetach":true}),
            )
            .await?;
        page.context = field(&context, "browserContextId")?.into();
        let target = page
            .send(
                None,
                "Target.createTarget",
                json!({"url":"about:blank", "browserContextId":page.context}),
            )
            .await?;
        let target = page
            .send(
                None,
                "Target.attachToTarget",
                json!({"targetId":field(&target,"targetId")?,"flatten":true}),
            )
            .await?;
        page.session = field(&target, "sessionId")?.into();
        page.install().await?;
        Ok(page)
    }
}

fn browser_executable() -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("CHROMIUM_EXECUTABLE") {
        return Ok(path.into());
    }
    let root = std::env::var_os("PLAYWRIGHT_BROWSERS_PATH")
        .map(PathBuf::from)
        .or_else(|| {
            if cfg!(windows) {
                std::env::var_os("LOCALAPPDATA")
                    .map(|root| PathBuf::from(root).join("ms-playwright"))
            } else {
                Some(PathBuf::from("/ms-playwright"))
            }
        })
        .context("cannot locate the Chromium browser directory")?;
    let relative = if cfg!(windows) {
        "chrome-win/headless_shell.exe"
    } else {
        "chrome-linux/headless_shell"
    };
    let path = root.join("chromium_headless_shell-1148").join(relative);
    ensure!(
        path.is_file(),
        "Chromium headless shell 1148 was not found at {}",
        path.display()
    );
    Ok(path)
}

#[derive(Default)]
struct Navigation {
    tracking: bool,
    frame: String,
    pending: HashSet<(String, String)>,
    idle_since: Option<Instant>,
    status: Option<u16>,
}

pub(crate) struct Page<'a> {
    socket: WebSocketStream<MaybeTlsStream<TcpStream>>,
    next_id: u64,
    allowed: &'a [String],
    context: String,
    session: String,
    navigation: Navigation,
}

impl Page<'_> {
    async fn post(&mut self, session: Option<&str>, method: &str, params: Value) -> Result<u64> {
        self.next_id += 1;
        let mut message = json!({"id":self.next_id,"method":method,"params":params});
        if let Some(session) = session {
            message["sessionId"] = json!(session);
        }
        self.socket
            .send(Message::Text(message.to_string().into()))
            .await?;
        Ok(self.next_id)
    }

    async fn send(&mut self, session: Option<&str>, method: &str, params: Value) -> Result<Value> {
        let id = self.post(session, method, params).await?;
        loop {
            let mut message = self.receive().await?;
            if message["id"].as_u64() == Some(id) {
                if let Some(error) = message["error"]["message"].as_str() {
                    bail!("{error}");
                }
                return Ok(message["result"].take());
            }
        }
    }

    async fn command(&mut self, method: &str, params: Value) -> Result<Value> {
        let session = self.session.clone();
        self.send(Some(&session), method, params).await
    }

    async fn receive(&mut self) -> Result<Value> {
        loop {
            let message = self
                .socket
                .next()
                .await
                .context("Chromium disconnected")??;
            if let Message::Text(text) = message {
                let value = serde_json::from_str(&text)?;
                self.event(&value).await?;
                return Ok(value);
            }
        }
    }

    async fn event(&mut self, value: &Value) -> Result<()> {
        let session = value["sessionId"].as_str();
        let params = &value["params"];
        match value["method"].as_str() {
            Some("Fetch.requestPaused") => {
                let url = field(&params["request"], "url")?;
                let request = field(params, "requestId")?;
                let allowed = url.starts_with("data:")
                    || url.starts_with("blob:")
                    || params.get("redirectedRequestId").is_some()
                    || origin(url).is_some_and(|origin| self.allowed.contains(&origin));
                if allowed {
                    self.post(
                        session,
                        "Fetch.continueRequest",
                        json!({"requestId":request}),
                    )
                    .await?;
                } else {
                    self.post(
                        session,
                        "Fetch.failRequest",
                        json!({"requestId":request,"errorReason":"Failed"}),
                    )
                    .await?;
                }
            }
            Some("Target.attachedToTarget") if session.is_some() => {
                let attached = Some(field(params, "sessionId")?);
                if params["targetInfo"]["type"] == "iframe" {
                    self.post(attached, "Network.enable", json!({})).await?;
                    self.post(
                        attached,
                        "Network.setCacheDisabled",
                        json!({"cacheDisabled":true}),
                    )
                    .await?;
                    self.post(
                        attached,
                        "Fetch.enable",
                        json!({"patterns":[{"urlPattern":"*"}]}),
                    )
                    .await?;
                    self.post(attached, "Target.setAutoAttach", auto_attach())
                        .await?;
                }
                self.post(attached, "Runtime.runIfWaitingForDebugger", json!({}))
                    .await?;
            }
            Some("Network.requestWillBeSent") if self.navigation.tracking => {
                let document_url = params["documentURL"].as_str().unwrap_or_default();
                let request_url = params["request"]["url"].as_str().unwrap_or_default();
                if !request_url.ends_with("/favicon.ico") || request_url == document_url {
                    self.navigation
                        .pending
                        .insert(request_key(session, params)?);
                    self.navigation.idle_since = None;
                }
            }
            Some("Network.loadingFinished" | "Network.loadingFailed")
                if self.navigation.tracking =>
            {
                if self
                    .navigation
                    .pending
                    .remove(&request_key(session, params)?)
                    && self.navigation.pending.is_empty()
                {
                    self.navigation.idle_since = Some(Instant::now());
                }
            }
            Some("Network.responseReceived")
                if self.navigation.tracking
                    && params["type"] == "Document"
                    && params["frameId"] == self.navigation.frame =>
            {
                self.navigation.status = params["response"]["status"]
                    .as_u64()
                    .and_then(|code| code.try_into().ok());
            }
            _ => {}
        }
        Ok(())
    }

    async fn install(&mut self) -> Result<()> {
        self.command("Page.enable", json!({})).await?;
        self.command("Runtime.enable", json!({})).await?;
        self.command("Network.enable", json!({})).await?;
        self.command(
            "Security.setIgnoreCertificateErrors",
            json!({"ignore":true}),
        )
        .await?;
        self.command(
            "Emulation.setFocusEmulationEnabled",
            json!({"enabled":true}),
        )
        .await?;
        self.command(
            "Emulation.setDeviceMetricsOverride",
            json!({
                "mobile":false,"width":794,"height":1122,"screenWidth":794,"screenHeight":1122,
                "deviceScaleFactor":1,"screenOrientation":{"angle":0,"type":"landscapePrimary"}
            }),
        )
        .await?;
        self.command(
            "Emulation.setEmulatedMedia",
            json!({"media":"","features":[
                {"name":"prefers-color-scheme","value":"light"},
                {"name":"prefers-reduced-motion","value":"no-preference"},
                {"name":"forced-colors","value":"none"},
                {"name":"prefers-contrast","value":"no-preference"}
            ]}),
        )
        .await?;
        self.command("Page.setFontFamilies", fonts()).await?;
        self.command("Target.setAutoAttach", auto_attach()).await?;
        self.command("Network.setCacheDisabled", json!({"cacheDisabled":true}))
            .await?;
        self.command("Fetch.enable", json!({"patterns":[{"urlPattern":"*"}]}))
            .await?;
        let frame_tree = self.command("Page.getFrameTree", json!({})).await?;
        self.navigation.frame = field(&frame_tree["frameTree"]["frame"], "id")?.into();
        Ok(())
    }

    pub(crate) async fn navigate(&mut self, url: &str) -> Result<Option<u16>> {
        self.navigation.tracking = true;
        self.navigation.idle_since = Some(Instant::now());
        let frame = self.navigation.frame.clone();
        let result = self
            .command(
                "Page.navigate",
                json!({"url":url,"frameId":frame,"referrerPolicy":"unsafeUrl"}),
            )
            .await?;
        if let Some(error) = result["errorText"].as_str() {
            bail!("{error} at {url}");
        }
        loop {
            if let Some(start) = self.navigation.idle_since {
                tokio::select! {
                    biased;
                    message = self.receive() => { message?; }
                    () = sleep_until(start + NETWORK_IDLE) => break,
                }
            } else {
                self.receive().await?;
            }
        }
        self.navigation.tracking = false;
        Ok(self.navigation.status)
    }

    pub(crate) async fn height(&mut self) -> Result<u64> {
        self.command(
            "Runtime.evaluate",
            json!({"expression":"document.fonts.ready","awaitPromise":true}),
        )
        .await?;
        let result = self.command("Runtime.evaluate", json!({
            "expression":"(() => { const el = document.querySelector('.resume-print-only .resume-paper-inner > div'); return el ? Math.max(el.scrollHeight, el.offsetHeight) : 0; })()",
            "returnByValue":true
        })).await?;
        result["result"]["value"]
            .as_u64()
            .context("could not measure resume height")
    }

    pub(crate) async fn pdf(&mut self) -> Result<Vec<u8>> {
        use base64::Engine;
        use base64::engine::general_purpose::STANDARD;

        let result = self.command("Page.printToPDF", json!({
            "transferMode":"ReturnAsStream","landscape":false,"displayHeaderFooter":false,
            "printBackground":true,"scale":1,"paperWidth":8.27,"paperHeight":11.7,
            "marginTop":0,"marginBottom":0,"marginLeft":0,"marginRight":0,"pageRanges":"",
            "preferCSSPageSize":true,"generateTaggedPDF":false,"generateDocumentOutline":false
        })).await?;
        let handle = field(&result, "stream")?;
        let mut bytes = Vec::new();
        loop {
            let chunk = self.command("IO.read", json!({"handle":handle})).await?;
            let data = field(&chunk, "data")?;
            if chunk["base64Encoded"] == true {
                bytes.extend(STANDARD.decode(data)?);
            } else {
                bytes.extend_from_slice(data.as_bytes());
            }
            if chunk["eof"] == true {
                break;
            }
        }
        self.command("IO.close", json!({"handle":handle})).await?;
        Ok(bytes)
    }

    pub(crate) async fn close(mut self) {
        let context = self.context.clone();
        let _ = self
            .send(
                None,
                "Target.disposeBrowserContext",
                json!({"browserContextId":context}),
            )
            .await;
    }
}

fn field<'a>(value: &'a Value, name: &str) -> Result<&'a str> {
    value[name]
        .as_str()
        .ok_or_else(|| anyhow!("missing Chromium field {name}"))
}

fn request_key(session: Option<&str>, params: &Value) -> Result<(String, String)> {
    Ok((
        session.unwrap_or_default().into(),
        field(params, "requestId")?.into(),
    ))
}

fn auto_attach() -> Value {
    json!({"autoAttach":true,"waitForDebuggerOnStart":true,"flatten":true})
}

fn fonts() -> Value {
    let mut params = json!({"fontFamilies":{
        "standard":"Times New Roman", "serif":"Times New Roman", "sansSerif":"Arial",
        "fixed":if cfg!(windows) {"Consolas"} else {"Monospace"},
        "cursive":"Comic Sans MS", "fantasy":"Impact"
    }});
    if cfg!(windows) {
        params["forScripts"] = json!([
            {"script":"cyrl","fontFamilies":{"standard":"Times New Roman","fixed":"Courier New","serif":"Times New Roman","sansSerif":"Arial"}},
            {"script":"arab","fontFamilies":{"fixed":"Courier New","sansSerif":"Segoe UI"}},
            {"script":"grek","fontFamilies":{"standard":"Times New Roman","fixed":"Courier New","serif":"Times New Roman","sansSerif":"Arial"}},
            {"script":"jpan","fontFamilies":{"standard":",Meiryo,Yu Gothic","fixed":"MS Gothic","serif":",Yu Mincho,MS PMincho","sansSerif":",Meiryo,Yu Gothic"}},
            {"script":"hang","fontFamilies":{"standard":"Malgun Gothic","fixed":"Gulimche","serif":"Batang","sansSerif":"Malgun Gothic","cursive":"Gungsuh"}},
            {"script":"hans","fontFamilies":{"standard":"Microsoft YaHei","fixed":"NSimsun","serif":"Simsun","sansSerif":"Microsoft YaHei","cursive":"KaiTi"}},
            {"script":"hant","fontFamilies":{"standard":"Microsoft JhengHei","fixed":"MingLiU","serif":"PMingLiU","sansSerif":"Microsoft JhengHei","cursive":"DFKai-SB"}}
        ]);
    }
    params
}
