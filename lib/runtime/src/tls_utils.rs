// SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Shared TLS utilities for the Dynamo runtime.
//!
//! Provides helpers for loading PEM certificates and building rustls
//! `ServerConfig` / `ClientConfig` objects used by the NATS transport
//! and the TCP request-plane.

use std::{
    fmt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, TryLockError},
    time::{Duration, Instant, SystemTime},
};

use anyhow::{Context, Result};
use arc_swap::ArcSwap;
use rustls::{ClientConfig, RootCertStore, ServerConfig, SignatureScheme, sign::CertifiedKey};
use rustls_pemfile::{certs, private_key};

/// TLS handshake timeout, configurable via `DYN_TCP_TLS_HANDSHAKE_TIMEOUT_SECS` (default: 3s).
pub fn handshake_timeout() -> std::time::Duration {
    use crate::config::environment_names::tcp_response_stream::tls as env;
    let secs = std::env::var(env::DYN_TCP_TLS_HANDSHAKE_TIMEOUT_SECS)
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(3);
    std::time::Duration::from_secs(secs)
}

/// Build a rustls `ServerConfig` from PEM certificate and key files.
///
/// When `client_ca_cert_path` is `Some`, the server requires clients to present
/// a certificate signed by that CA (mutual TLS). When `None`, client
/// certificates are not requested. The server identity is checked at most once
/// every 30 seconds and atomically replaced after a valid cert/key pair is observed.
pub fn server_tls_config(
    cert_path: &Path,
    key_path: &Path,
    client_ca_cert_path: Option<&Path>,
) -> Result<ServerConfig> {
    let cert_resolver = Arc::new(ReloadingCertifiedKey::new(cert_path, key_path)?);

    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let builder = ServerConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .context("configuring TLS protocol versions")?;

    let config = if let Some(ca_path) = client_ca_cert_path {
        let ca_pem = std::fs::read(ca_path)
            .with_context(|| format!("reading client CA cert: {}", ca_path.display()))?;
        let ca_certs = certs(&mut ca_pem.as_slice())
            .collect::<Result<Vec<_>, _>>()
            .context("parsing client CA certificate PEM")?;
        let mut client_roots = RootCertStore::empty();
        for cert in ca_certs {
            client_roots
                .add(cert)
                .context("adding client CA certificate to root store")?;
        }
        let verifier = rustls::server::WebPkiClientVerifier::builder_with_provider(
            Arc::new(client_roots),
            provider,
        )
        .build()
        .context("building client certificate verifier")?;
        builder
            .with_client_cert_verifier(verifier)
            .with_cert_resolver(cert_resolver)
    } else {
        builder
            .with_no_client_auth()
            .with_cert_resolver(cert_resolver)
    };

    Ok(config)
}

/// Build a rustls `ClientConfig` for outbound TLS connections.
///
/// - `ca_cert_path`: trust this CA for verifying the server certificate.
///   When `None`, the root store is empty — supply a CA cert or use `insecure`.
/// - `insecure`: skip certificate verification entirely. **Dev/test only.**
/// - `client_cert_path` + `client_key_path`: when both are `Some`, the client
///   presents this certificate to the server (mutual TLS). The client identity
///   is checked at most once every 30 seconds and atomically replaced after a
///   valid cert/key pair is observed.
pub fn client_tls_config(
    ca_cert_path: Option<&Path>,
    insecure: bool,
    client_cert_path: Option<&Path>,
    client_key_path: Option<&Path>,
) -> Result<ClientConfig> {
    if client_cert_path.is_some() != client_key_path.is_some() {
        anyhow::bail!("client_cert_path and client_key_path must both be set or both be unset");
    }

    let provider = Arc::new(rustls::crypto::ring::default_provider());

    if insecure {
        tracing::info!("TLS: certificate verification disabled (insecure mode)");
        let builder = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .context("configuring TLS protocol versions")?
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(NoVerifier));
        let config = match (client_cert_path, client_key_path) {
            (Some(cp), Some(kp)) => {
                builder.with_client_cert_resolver(Arc::new(ReloadingCertifiedKey::new(cp, kp)?))
            }
            _ => builder.with_no_client_auth(),
        };
        return Ok(config);
    }

    let mut root_store = RootCertStore::empty();
    if let Some(ca_path) = ca_cert_path {
        let ca_pem = std::fs::read(ca_path)
            .with_context(|| format!("reading CA cert: {}", ca_path.display()))?;
        let ca_certs = certs(&mut ca_pem.as_slice())
            .collect::<Result<Vec<_>, _>>()
            .context("parsing CA certificate PEM")?;
        for cert in ca_certs {
            root_store
                .add(cert)
                .context("adding CA certificate to root store")?;
        }
        if root_store.is_empty() {
            anyhow::bail!(
                "CA certificate store is empty after parsing {}; \
                 ensure the file contains at least one valid PEM certificate",
                ca_path.display()
            );
        }
    }

    let builder = ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .context("configuring TLS protocol versions")?
        .with_root_certificates(root_store);

    let config = match (client_cert_path, client_key_path) {
        (Some(cp), Some(kp)) => {
            builder.with_client_cert_resolver(Arc::new(ReloadingCertifiedKey::new(cp, kp)?))
        }
        _ => builder.with_no_client_auth(),
    };

    Ok(config)
}

struct ReloadingCertifiedKey {
    cert_path: PathBuf,
    key_path: PathBuf,
    current: ArcSwap<CertifiedKey>,
    reload_state: Mutex<ReloadState>,
}

struct ReloadState {
    fingerprint: IdentityFingerprint,
    last_checked: Instant,
}

struct LoadedIdentity {
    fingerprint: IdentityFingerprint,
    certified_key: Arc<CertifiedKey>,
}

#[derive(Debug, Eq, PartialEq)]
struct IdentityFingerprint {
    cert: FileFingerprint,
    key: FileFingerprint,
}

#[derive(Debug, Eq, PartialEq)]
struct FileFingerprint {
    canonical_path: PathBuf,
    len: u64,
    modified: SystemTime,
}

impl fmt::Debug for ReloadingCertifiedKey {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ReloadingCertifiedKey")
            .field("cert_path", &self.cert_path)
            .field("key_path", &self.key_path)
            .finish_non_exhaustive()
    }
}

impl ReloadingCertifiedKey {
    const RELOAD_CHECK_INTERVAL: Duration = Duration::from_secs(30);

    fn new(cert_path: &Path, key_path: &Path) -> Result<Self> {
        let cert_path = cert_path.to_path_buf();
        let key_path = key_path.to_path_buf();
        let loaded = Self::load(&cert_path, &key_path)?;
        Ok(Self {
            cert_path,
            key_path,
            current: ArcSwap::from(loaded.certified_key),
            reload_state: Mutex::new(ReloadState {
                fingerprint: loaded.fingerprint,
                last_checked: Instant::now(),
            }),
        })
    }

    fn load(cert_path: &Path, key_path: &Path) -> Result<LoadedIdentity> {
        let fingerprint = IdentityFingerprint::new(cert_path, key_path)?;
        let cert_pem = std::fs::read(cert_path)
            .with_context(|| format!("reading cert: {}", cert_path.display()))?;
        let key_pem = std::fs::read(key_path)
            .with_context(|| format!("reading key: {}", key_path.display()))?;
        let certified_key = load_certified_key(&cert_pem, &key_pem)?;
        Ok(LoadedIdentity {
            fingerprint,
            certified_key: Arc::new(certified_key),
        })
    }

    fn resolve_key(&self) -> Arc<CertifiedKey> {
        self.refresh()
    }

    fn refresh(&self) -> Arc<CertifiedKey> {
        // Never make a handshake wait for another handshake's filesystem check.
        // The current identity remains available through ArcSwap while one caller
        // serializes and performs the rate-limited reload.
        let mut state = match self.reload_state.try_lock() {
            Ok(state) => state,
            Err(TryLockError::WouldBlock) => return self.current.load_full(),
            Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
        };
        if state.last_checked.elapsed() < Self::RELOAD_CHECK_INTERVAL {
            return self.current.load_full();
        }
        state.last_checked = Instant::now();

        let fingerprint = match IdentityFingerprint::new(&self.cert_path, &self.key_path) {
            Ok(fingerprint) => fingerprint,
            Err(error) => {
                tracing::warn!(
                    %error,
                    "failed to inspect rotated TLS identity; keeping the last valid identity"
                );
                return self.current.load_full();
            }
        };
        if state.fingerprint == fingerprint {
            return self.current.load_full();
        }

        match Self::load(&self.cert_path, &self.key_path) {
            Ok(reloaded) => {
                self.current.store(reloaded.certified_key);
                state.fingerprint = reloaded.fingerprint;
                tracing::info!("reloaded rotated TLS certificate and private key");
            }
            Err(error) => {
                tracing::warn!(
                    %error,
                    "failed to load rotated TLS certificate and private key; keeping the last valid identity"
                );
            }
        }
        self.current.load_full()
    }

    #[cfg(test)]
    fn mark_reload_due(&self) {
        let mut state = self
            .reload_state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.last_checked = Instant::now() - Self::RELOAD_CHECK_INTERVAL;
    }
}

impl IdentityFingerprint {
    fn new(cert_path: &Path, key_path: &Path) -> Result<Self> {
        Ok(Self {
            cert: FileFingerprint::new(cert_path)?,
            key: FileFingerprint::new(key_path)?,
        })
    }
}

impl FileFingerprint {
    fn new(path: &Path) -> Result<Self> {
        let canonical_path = std::fs::canonicalize(path)
            .with_context(|| format!("resolving TLS file path: {}", path.display()))?;
        let metadata = std::fs::metadata(&canonical_path)
            .with_context(|| format!("reading TLS file metadata: {}", path.display()))?;
        let modified = metadata
            .modified()
            .with_context(|| format!("reading TLS file modification time: {}", path.display()))?;
        Ok(Self {
            canonical_path,
            len: metadata.len(),
            modified,
        })
    }
}

impl rustls::server::ResolvesServerCert for ReloadingCertifiedKey {
    fn resolve(&self, _client_hello: rustls::server::ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        Some(self.resolve_key())
    }
}

impl rustls::client::ResolvesClientCert for ReloadingCertifiedKey {
    fn resolve(
        &self,
        _root_hint_subjects: &[&[u8]],
        _sigschemes: &[SignatureScheme],
    ) -> Option<Arc<CertifiedKey>> {
        Some(self.resolve_key())
    }

    fn has_certs(&self) -> bool {
        true
    }
}

fn load_certified_key(cert_pem: &[u8], key_pem: &[u8]) -> Result<CertifiedKey> {
    let mut cert_reader = cert_pem;
    let cert_chain = certs(&mut cert_reader)
        .collect::<Result<Vec<_>, _>>()
        .context("parsing certificate PEM")?;
    let mut key_reader = key_pem;
    let key = private_key(&mut key_reader)
        .context("parsing private key PEM")?
        .context("no private key found in PEM")?;
    let signing_key =
        rustls::crypto::ring::sign::any_supported_type(&key).context("loading TLS private key")?;
    let certified_key = CertifiedKey::new(cert_chain, signing_key);
    certified_key
        .keys_match()
        .context("TLS certificate and private key do not match")?;
    Ok(certified_key)
}

/// Certificate verifier that accepts any certificate.
/// **Only for development/testing. Never use in production.**
#[derive(Debug)]
struct NoVerifier;

impl rustls::client::danger::ServerCertVerifier for NoVerifier {
    fn verify_server_cert(
        &self,
        _end_entity: &rustls::pki_types::CertificateDer<'_>,
        _intermediates: &[rustls::pki_types::CertificateDer<'_>],
        _server_name: &rustls::pki_types::ServerName<'_>,
        _ocsp_response: &[u8],
        _now: rustls::pki_types::UnixTime,
    ) -> std::result::Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _message: &[u8],
        _cert: &rustls::pki_types::CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _message: &[u8],
        _cert: &rustls::pki_types::CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    fn make_cert_files() -> (NamedTempFile, NamedTempFile) {
        let key_pair = rcgen::KeyPair::generate().unwrap();
        let cert = rcgen::CertificateParams::new(vec!["localhost".to_string()])
            .unwrap()
            .self_signed(&key_pair)
            .unwrap();
        let mut cert_file = NamedTempFile::new().unwrap();
        cert_file.write_all(cert.pem().as_bytes()).unwrap();
        let mut key_file = NamedTempFile::new().unwrap();
        key_file
            .write_all(key_pair.serialize_pem().as_bytes())
            .unwrap();
        (cert_file, key_file)
    }

    fn make_cert_pem() -> (String, String) {
        let key_pair = rcgen::KeyPair::generate().unwrap();
        let cert = rcgen::CertificateParams::new(vec!["localhost".to_string()])
            .unwrap()
            .self_signed(&key_pair)
            .unwrap();
        (cert.pem(), key_pair.serialize_pem())
    }

    #[test]
    fn server_config_roundtrip() {
        let (cert, key) = make_cert_files();
        server_tls_config(cert.path(), key.path(), None).unwrap();
    }

    #[test]
    fn client_config_with_mtls() {
        let (cert, key) = make_cert_files();
        client_tls_config(
            Some(cert.path()),
            false,
            Some(cert.path()),
            Some(key.path()),
        )
        .unwrap();
    }

    #[test]
    fn client_config_partial_mtls_errors() {
        let (cert, _) = make_cert_files();
        assert!(client_tls_config(Some(cert.path()), false, Some(cert.path()), None).is_err());
    }

    #[test]
    fn client_config_empty_ca_errors() {
        let empty = NamedTempFile::new().unwrap();
        assert!(
            client_tls_config(Some(empty.path()), false, None, None)
                .unwrap_err()
                .to_string()
                .contains("CA certificate store is empty")
        );
    }

    #[test]
    fn client_config_missing_ca_errors() {
        assert!(
            client_tls_config(
                Some(std::path::Path::new("/nonexistent/ca.pem")),
                false,
                None,
                None
            )
            .unwrap_err()
            .to_string()
            .contains("reading CA cert")
        );
    }

    #[test]
    fn certified_key_reloads_rotated_files() {
        let (cert_pem, key_pem) = make_cert_pem();
        let mut cert_file = NamedTempFile::new().unwrap();
        cert_file.write_all(cert_pem.as_bytes()).unwrap();
        let mut key_file = NamedTempFile::new().unwrap();
        key_file.write_all(key_pem.as_bytes()).unwrap();

        let resolver = ReloadingCertifiedKey::new(cert_file.path(), key_file.path()).unwrap();
        let original = resolver.resolve_key();

        let (rotated_cert_pem, rotated_key_pem) = make_cert_pem();
        std::fs::write(cert_file.path(), rotated_cert_pem).unwrap();
        std::fs::write(key_file.path(), rotated_key_pem).unwrap();

        resolver.mark_reload_due();
        let rotated = resolver.resolve_key();
        assert_ne!(original.cert, rotated.cert);
    }

    #[test]
    fn certified_key_resolve_does_not_wait_for_concurrent_reload() {
        let (cert, key) = make_cert_files();
        let resolver = ReloadingCertifiedKey::new(cert.path(), key.path()).unwrap();
        let expected = resolver.resolve_key();

        let _reload_guard = resolver.reload_state.lock().unwrap();
        let resolved = resolver.resolve_key();

        assert_eq!(expected.cert, resolved.cert);
    }

    #[cfg(unix)]
    #[test]
    fn certified_key_reloads_symlinked_certificate_generation() {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir().unwrap();
        let first_generation = root.path().join("server-1");
        let second_generation = root.path().join("server-2");
        std::fs::create_dir(&first_generation).unwrap();
        std::fs::create_dir(&second_generation).unwrap();

        let (first_cert, first_key) = make_cert_pem();
        std::fs::write(first_generation.join("cert.pem"), first_cert).unwrap();
        std::fs::write(first_generation.join("key.pem"), first_key).unwrap();
        let (second_cert, second_key) = make_cert_pem();
        std::fs::write(second_generation.join("cert.pem"), second_cert).unwrap();
        std::fs::write(second_generation.join("key.pem"), second_key).unwrap();

        let current = root.path().join("server");
        symlink(&first_generation, &current).unwrap();
        let resolver =
            ReloadingCertifiedKey::new(&current.join("cert.pem"), &current.join("key.pem"))
                .unwrap();
        let original = resolver.resolve_key();

        std::fs::remove_file(&current).unwrap();
        symlink(&second_generation, &current).unwrap();

        resolver.mark_reload_due();
        let rotated = resolver.resolve_key();
        assert_ne!(original.cert, rotated.cert);
    }

    #[test]
    fn certified_key_keeps_last_valid_identity_during_partial_rotation() {
        let (cert_pem, key_pem) = make_cert_pem();
        let mut cert_file = NamedTempFile::new().unwrap();
        cert_file.write_all(cert_pem.as_bytes()).unwrap();
        let mut key_file = NamedTempFile::new().unwrap();
        key_file.write_all(key_pem.as_bytes()).unwrap();

        let resolver = ReloadingCertifiedKey::new(cert_file.path(), key_file.path()).unwrap();
        let original = resolver.resolve_key();

        let (rotated_cert_pem, _) = make_cert_pem();
        std::fs::write(cert_file.path(), rotated_cert_pem).unwrap();

        resolver.mark_reload_due();
        let after_partial_rotation = resolver.resolve_key();
        assert_eq!(original.cert, after_partial_rotation.cert);
    }
}
