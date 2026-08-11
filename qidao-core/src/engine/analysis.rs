use super::line_reader::{BoundedLineReader, LineReadError};
use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::process::Stdio;
use tokio::io::AsyncWriteExt;
use tokio::process::{Child, Command};

const CHILD_SHUTDOWN_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(2);
const ANALYSIS_RESULT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(2);
const ANALYSIS_LINE_LIMIT: usize = 1024 * 1024;
const ANALYSIS_STDERR_LIMIT: usize = 64 * 1024;

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct AnalysisQuery {
    pub id: String,
    pub moves: Vec<(String, String)>, // (color, move)
    pub initial_stones: Vec<(String, String)>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub initial_player: Option<String>,
    pub rules: String,
    pub komi: f64,
    pub board_x_size: u32,
    pub board_y_size: u32,
    pub analyze_turns: Vec<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_visits: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_time: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub report_during_search_every: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub include_ownership: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub include_policy: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub priority: Option<i32>,
}

pub struct AnalysisClient {
    child: Child,
    stdin: tokio::process::ChildStdin,
    stdout_reader: BoundedLineReader<tokio::process::ChildStdout>,
    stderr_reader: BoundedLineReader<tokio::process::ChildStderr>,
}

impl AnalysisClient {
    pub async fn start(executable: &str, args: &[String]) -> Result<Self> {
        let mut child = Command::new(executable)
            .args(args)
            .current_dir(std::env::temp_dir())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
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
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| anyhow!("Failed to open stderr"))?;
        let stdout_reader = BoundedLineReader::new(stdout, ANALYSIS_LINE_LIMIT);
        let stderr_reader = BoundedLineReader::new(stderr, ANALYSIS_STDERR_LIMIT);

        Ok(Self {
            child,
            stdin,
            stdout_reader,
            stderr_reader,
        })
    }

    pub async fn send_query(&mut self, query: &AnalysisQuery) -> Result<()> {
        let json = serde_json::to_string(query)?;
        let line = format!("{}\n", json);
        self.stdin.write_all(line.as_bytes()).await?;
        self.stdin.flush().await?;
        Ok(())
    }

    pub async fn read_response(&mut self) -> Result<Value> {
        let deadline = tokio::time::Instant::now() + ANALYSIS_RESULT_TIMEOUT;
        let line = match self.stdout_reader.read_line_until(deadline).await {
            Ok(Some(line)) => line,
            Ok(None) => return Err(anyhow!("Engine closed stdout")),
            Err(error) => {
                let framing_violation = error.is_line_too_long();
                let error = anyhow!(error);
                if framing_violation {
                    self.terminate_and_reap().await?;
                }
                return Err(error);
            }
        };
        let val: Value = serde_json::from_str(&line)?;
        Ok(val)
    }

    pub async fn read_stderr_line(&mut self) -> Result<Option<String>> {
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_millis(10);
        match self.stderr_reader.read_line_until(deadline).await {
            Ok(Some(line)) => Ok(Some(line.trim().to_string())),
            Ok(None) | Err(LineReadError::Timeout) => Ok(None),
            Err(error) => {
                let framing_violation = error.is_line_too_long();
                let error = anyhow!(error);
                if framing_violation {
                    self.terminate_and_reap().await?;
                }
                Err(error)
            }
        }
    }

    async fn terminate_and_reap(&mut self) -> Result<()> {
        if self.child.try_wait()?.is_some() {
            return Ok(());
        }
        let kill_error = self.child.start_kill().err();
        match tokio::time::timeout(CHILD_SHUTDOWN_TIMEOUT, self.child.wait()).await {
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

    pub async fn stop(mut self) -> Result<()> {
        drop(self.stdin);
        let deadline = tokio::time::Instant::now() + CHILD_SHUTDOWN_TIMEOUT;
        if let Ok(status) = tokio::time::timeout_at(deadline, self.child.wait()).await {
            status?;
            return Ok(());
        }

        let kill_error = self.child.start_kill().err();
        match tokio::time::timeout(CHILD_SHUTDOWN_TIMEOUT, self.child.wait()).await {
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
    async fn oversized_response_is_rejected_and_child_is_reaped() {
        let args = vec![
            "if=/dev/zero".into(),
            format!("bs={}", ANALYSIS_LINE_LIMIT + 1),
            "count=1".into(),
        ];
        let mut client = AnalysisClient::start("/bin/dd", &args).await.unwrap();
        let pid = client.child.id().unwrap();
        let error = client.read_response().await.unwrap_err();
        assert!(error.to_string().contains("line exceeds"));
        assert!(!process_is_alive(pid));
    }

    #[tokio::test]
    async fn stderr_partial_line_survives_poll_timeout() {
        let args = vec![
            "-c".into(),
            "printf partial >&2; sleep 0.05; printf ' line\\n' >&2; exec sleep 60".into(),
        ];
        let mut client = AnalysisClient::start("/bin/sh", &args).await.unwrap();
        assert_eq!(client.read_stderr_line().await.unwrap(), None);
        tokio::time::sleep(Duration::from_millis(80)).await;
        assert_eq!(
            client.read_stderr_line().await.unwrap().as_deref(),
            Some("partial line")
        );
        client.stop().await.unwrap();
    }

    #[tokio::test]
    async fn stop_is_bounded_and_reaps_child_that_ignores_stdin_eof() {
        let args = vec!["-f".into(), "/dev/null".into()];
        let client = AnalysisClient::start("/usr/bin/tail", &args).await.unwrap();
        let pid = client.child.id().unwrap();

        tokio::time::timeout(Duration::from_secs(3), client.stop())
            .await
            .expect("analysis stop exceeded its shutdown deadline")
            .unwrap();

        assert!(!std::process::Command::new("/bin/kill")
            .args(["-0", &pid.to_string()])
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap()
            .success());
    }
}
