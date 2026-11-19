use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::Router;
use axum::extract::{Request, State};
use axum::response::Response;
use resume_render_service::port::{Service, port_from_env};
use tokio::sync::oneshot;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let port = port_from_env()?;
    let listener = tokio::net::TcpListener::bind(SocketAddr::from(([0, 0, 0, 0], port))).await?;
    let app = Router::new()
        .fallback(handle)
        .with_state(Arc::new(Service::from_env()));
    println!("resume-render listening on {port}");

    let (stopping, stop) = oneshot::channel();
    let server = axum::serve(listener, app)
        .with_graceful_shutdown(async {
            shutdown_signal().await;
            let _ = stopping.send(());
        })
        .into_future();
    tokio::pin!(server);
    tokio::select! {
        result = &mut server => result?,
        _ = stop => {
            if let Ok(result) = tokio::time::timeout(Duration::from_secs(3), server).await {
                result?;
            }
        }
    }
    Ok(())
}

async fn handle(State(service): State<Arc<Service>>, request: Request) -> Response {
    service.handle(request).await
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        if let Ok(mut term) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {}
                _ = term.recv() => {}
            }
            return;
        }
    }
    let _ = tokio::signal::ctrl_c().await;
}
