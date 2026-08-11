use std::fmt;
use std::io;
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::time::Instant;

#[derive(Debug)]
pub(crate) enum LineReadError {
    Io(io::Error),
    Timeout,
    LineTooLong { limit: usize },
}

impl LineReadError {
    pub(crate) fn is_line_too_long(&self) -> bool {
        matches!(self, Self::LineTooLong { .. })
    }
}

impl fmt::Display for LineReadError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => error.fmt(formatter),
            Self::Timeout => write!(formatter, "line read timed out"),
            Self::LineTooLong { limit } => {
                write!(formatter, "line exceeds {limit}-byte limit")
            }
        }
    }
}

impl std::error::Error for LineReadError {}

impl From<io::Error> for LineReadError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// Cancellation-safe newline reader with persistent partial bytes and a hard
/// allocation limit. `fill_buf` is never consumed until its bytes have been
/// copied into `pending`, so a deadline cannot discard a partial engine line.
pub(crate) struct BoundedLineReader<R> {
    reader: BufReader<R>,
    pending: Vec<u8>,
    max_line_bytes: usize,
}

impl<R: AsyncRead + Unpin> BoundedLineReader<R> {
    pub(crate) fn new(reader: R, max_line_bytes: usize) -> Self {
        Self {
            reader: BufReader::new(reader),
            pending: Vec::new(),
            max_line_bytes,
        }
    }

    pub(crate) async fn read_line_until(
        &mut self,
        deadline: Instant,
    ) -> Result<Option<String>, LineReadError> {
        loop {
            let available = tokio::time::timeout_at(deadline, self.reader.fill_buf())
                .await
                .map_err(|_| LineReadError::Timeout)??;
            if available.is_empty() {
                return if self.pending.is_empty() {
                    Ok(None)
                } else {
                    self.finish_line().map(Some)
                };
            }

            let newline = available.iter().position(|byte| *byte == b'\n');
            let fragment_len = newline.unwrap_or(available.len());
            if fragment_len > self.max_line_bytes
                || self.pending.len() > self.max_line_bytes - fragment_len
            {
                self.pending.clear();
                return Err(LineReadError::LineTooLong {
                    limit: self.max_line_bytes,
                });
            }
            self.pending.extend_from_slice(&available[..fragment_len]);
            self.reader
                .consume(fragment_len + usize::from(newline.is_some()));
            if newline.is_some() {
                return self.finish_line().map(Some);
            }
        }
    }

    fn finish_line(&mut self) -> Result<String, LineReadError> {
        if self.pending.last() == Some(&b'\r') {
            self.pending.pop();
        }
        String::from_utf8(std::mem::take(&mut self.pending))
            .map_err(|error| LineReadError::Io(io::Error::new(io::ErrorKind::InvalidData, error)))
    }

    #[cfg(test)]
    pub(crate) fn buffered_byte_count(&self) -> usize {
        self.pending.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{duplex, AsyncWriteExt};
    use tokio::time::{Duration, Instant};

    #[tokio::test]
    async fn accepts_standard_katago_ownership_line() {
        let (mut writer, reader) = duplex(16 * 1024);
        let ownership = (0..361)
            .map(|index| if index % 2 == 0 { "0.25" } else { "-0.25" })
            .collect::<Vec<_>>()
            .join(",");
        let line = format!("{{\"id\":\"ordinary\",\"ownership\":[{ownership}]}}\n");
        writer.write_all(line.as_bytes()).await.unwrap();

        let mut reader = BoundedLineReader::new(reader, 1024 * 1024);
        let parsed = reader
            .read_line_until(Instant::now() + Duration::from_secs(1))
            .await
            .unwrap()
            .unwrap();
        assert!(parsed.starts_with("{\"id\":\"ordinary\""));
        assert!(parsed.ends_with("}"));
    }

    #[tokio::test]
    async fn preserves_partial_bytes_across_timeout_cancellation() {
        let (mut writer, reader) = duplex(64);
        let mut reader = BoundedLineReader::new(reader, 64);
        writer.write_all(b"partial").await.unwrap();

        let error = reader
            .read_line_until(Instant::now() + Duration::from_millis(20))
            .await
            .unwrap_err();
        assert!(matches!(error, LineReadError::Timeout));
        assert_eq!(reader.buffered_byte_count(), 7);

        writer.write_all(b" line\n").await.unwrap();
        let line = reader
            .read_line_until(Instant::now() + Duration::from_secs(1))
            .await
            .unwrap();
        assert_eq!(line.as_deref(), Some("partial line"));
    }

    #[tokio::test]
    async fn rejects_unterminated_line_without_exceeding_storage_limit() {
        let (mut writer, reader) = duplex(128);
        let mut reader = BoundedLineReader::new(reader, 32);
        writer.write_all(&[b'x'; 33]).await.unwrap();

        let error = reader
            .read_line_until(Instant::now() + Duration::from_secs(1))
            .await
            .unwrap_err();
        assert!(matches!(error, LineReadError::LineTooLong { limit: 32 }));
        assert!(reader.buffered_byte_count() <= 32);
    }
}
