use super::line_reader::BoundedLineReader;
use anyhow::{anyhow, Result};
use std::process::Stdio;
use tokio::io::AsyncWriteExt;
use tokio::process::{Child, Command};

const CHILD_SHUTDOWN_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(2);
// GTP commands are serialized and must finish write plus the complete response
// under one deadline. Lines are capped at 1 MiB and whole responses at 4 MiB.
const GTP_COMMAND_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);
const GTP_LINE_LIMIT: usize = 1024 * 1024;
const GTP_RESPONSE_LIMIT: usize = 4 * 1024 * 1024;

pub struct GtpClient {
    child: tokio::sync::Mutex<Child>,
    io: tokio::sync::Mutex<GtpIo>,
}

struct GtpIo {
    stdin: tokio::process::ChildStdin,
    stdout_reader: BoundedLineReader<tokio::process::ChildStdout>,
}

impl GtpClient {
    pub async fn start(executable: &str, args: &[String]) -> Result<Self> {
        let mut child = Command::new(executable)
            .args(args)
            .current_dir(std::env::temp_dir())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("Failed to open stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("Failed to open stdout"))?;
        let stdout_reader = BoundedLineReader::new(stdout, GTP_LINE_LIMIT);

        Ok(Self {
            child: tokio::sync::Mutex::new(child),
            io: tokio::sync::Mutex::new(GtpIo {
                stdin,
                stdout_reader,
            }),
        })
    }

    pub async fn send_command(&self, cmd: &str) -> Result<String> {
        self.send_command_until(cmd, tokio::time::Instant::now() + GTP_COMMAND_TIMEOUT)
            .await
    }

    async fn send_command_until(
        &self,
        cmd: &str,
        deadline: tokio::time::Instant,
    ) -> Result<String> {
        let io_result = async {
            let mut io = tokio::time::timeout_at(deadline, self.io.lock())
                .await
                .map_err(|_| anyhow!("GTP command timed out"))?;
            let cmd_line = format!("{}\n", cmd);
            tokio::time::timeout_at(deadline, io.stdin.write_all(cmd_line.as_bytes()))
                .await
                .map_err(|_| anyhow!("GTP command timed out"))??;
            tokio::time::timeout_at(deadline, io.stdin.flush())
                .await
                .map_err(|_| anyhow!("GTP command timed out"))??;

            let mut response = String::new();
            loop {
                let line = io
                    .stdout_reader
                    .read_line_until(deadline)
                    .await
                    .map_err(|error| match error {
                        super::line_reader::LineReadError::Timeout => {
                            anyhow!("GTP command timed out")
                        }
                        error => anyhow!(error),
                    })?
                    .ok_or_else(|| anyhow!("Engine closed stdout"))?;
                if line.trim().is_empty() {
                    if !response.is_empty() {
                        break;
                    }
                    continue;
                }
                let separator = usize::from(!response.is_empty());
                if response.len() > GTP_RESPONSE_LIMIT.saturating_sub(line.len() + separator) {
                    return Err(anyhow!(
                        "GTP response exceeds {GTP_RESPONSE_LIMIT}-byte limit"
                    ));
                }
                if separator == 1 {
                    response.push('\n');
                }
                response.push_str(&line);
            }
            Ok::<_, anyhow::Error>(response)
        }
        .await;

        let response = match io_result {
            Ok(response) => response,
            Err(error) => {
                // The I/O guard is out of scope before taking the child lock.
                let reap_error = self.terminate_and_reap().await.err();
                return match reap_error {
                    Some(reap_error) => Err(anyhow!("{error}; child cleanup failed: {reap_error}")),
                    None => Err(error),
                };
            }
        };

        if response.starts_with('=') {
            Ok(response[1..].trim().to_string())
        } else if response.starts_with('?') {
            Err(anyhow!("GTP Error: {}", response[1..].trim()))
        } else {
            Ok(response.trim().to_string())
        }
    }

    async fn terminate_and_reap(&self) -> Result<()> {
        let mut child = self.child.lock().await;
        if child.try_wait()?.is_some() {
            return Ok(());
        }
        let kill_error = child.start_kill().err();
        match tokio::time::timeout(CHILD_SHUTDOWN_TIMEOUT, child.wait()).await {
            Ok(Ok(_)) => Ok(()),
            Ok(Err(wait_error)) => match kill_error {
                Some(kill_error) => Err(anyhow!(
                    "failed to kill child: {kill_error}; failed to reap child: {wait_error}"
                )),
                None => Err(wait_error.into()),
            },
            Err(_) => match kill_error {
                Some(kill_error) => Err(anyhow!(
                    "failed to kill child: {kill_error}; timed out reaping child"
                )),
                None => Err(anyhow!("timed out reaping child")),
            },
        }
    }

    pub async fn stop(&self) -> Result<()> {
        let deadline = tokio::time::Instant::now() + CHILD_SHUTDOWN_TIMEOUT;
        let _ = tokio::time::timeout_at(deadline, self.send_command("quit")).await;
        let mut child = self.child.lock().await;

        if let Ok(status) = tokio::time::timeout_at(deadline, child.wait()).await {
            status?;
            return Ok(());
        }

        let kill_error = child.start_kill().err();
        match tokio::time::timeout(CHILD_SHUTDOWN_TIMEOUT, child.wait()).await {
            Ok(Ok(_)) => Ok(()),
            Ok(Err(wait_error)) => match kill_error {
                Some(kill_error) => Err(anyhow!(
                    "failed to kill child: {kill_error}; failed to reap child: {wait_error}"
                )),
                None => Err(wait_error.into()),
            },
            Err(_) => match kill_error {
                Some(kill_error) => Err(anyhow!(
                    "failed to kill child: {kill_error}; timed out reaping child"
                )),
                None => Err(anyhow!("timed out reaping child")),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn process_is_alive(pid: u32) -> bool {
        std::process::Command::new("/bin/kill")
            .args(["-0", &pid.to_string()])
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap()
            .success()
    }

    #[tokio::test]
    async fn ordinary_response_still_works() {
        let args = vec!["-c".into(), "read command; printf '= QiDao\\n\\n'".into()];
        let client = GtpClient::start("/bin/sh", &args).await.unwrap();
        assert_eq!(client.send_command("name").await.unwrap(), "QiDao");
        client.stop().await.unwrap();
    }

    #[tokio::test]
    async fn command_timeout_is_bounded_and_reaps_child() {
        let args = vec!["-f".into(), "/dev/null".into()];
        let client = GtpClient::start("/usr/bin/tail", &args).await.unwrap();
        let pid = client.child.lock().await.id().unwrap();
        let started = std::time::Instant::now();
        let error = client
            .send_command_until(
                "name",
                tokio::time::Instant::now() + Duration::from_millis(100),
            )
            .await
            .unwrap_err();
        assert!(error.to_string().contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(2));
        assert!(!process_is_alive(pid));
    }

    #[tokio::test]
    async fn oversized_continuous_output_is_rejected_and_reaped() {
        let args = vec![
            "if=/dev/zero".into(),
            format!("bs={}", GTP_LINE_LIMIT + 1),
            "count=1".into(),
        ];
        let client = GtpClient::start("/bin/dd", &args).await.unwrap();
        let pid = client.child.lock().await.id().unwrap();
        let error = client
            .send_command_until("name", tokio::time::Instant::now() + Duration::from_secs(2))
            .await
            .unwrap_err();
        assert!(error.to_string().contains("line exceeds"));
        assert!(!process_is_alive(pid));
    }

    #[tokio::test]
    async fn aggregate_response_limit_reaps_endless_short_lines() {
        let client = GtpClient::start("/usr/bin/yes", &["=0123456789".into()])
            .await
            .unwrap();
        let pid = client.child.lock().await.id().unwrap();
        let error = client
            .send_command_until("name", tokio::time::Instant::now() + Duration::from_secs(2))
            .await
            .unwrap_err();
        assert!(error.to_string().contains("response exceeds"));
        assert!(!process_is_alive(pid));
    }

    #[tokio::test]
    async fn stop_is_bounded_and_reaps_unresponsive_child() {
        let args = vec!["-f".into(), "/dev/null".into()];
        let client = GtpClient::start("/usr/bin/tail", &args).await.unwrap();
        let pid = client.child.lock().await.id().unwrap();

        tokio::time::timeout(Duration::from_secs(3), client.stop())
            .await
            .expect("GTP stop exceeded its shutdown deadline")
            .unwrap();

        assert!(!std::process::Command::new("/bin/kill")
            .args(["-0", &pid.to_string()])
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap()
            .success());
    }
}
