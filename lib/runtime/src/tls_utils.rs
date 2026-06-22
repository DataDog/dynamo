// SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Shared TLS utilities for the Dynamo runtime.
//!
//! Provides helpers for loading PEM certificates and building rustls
//! `ServerConfig` / `ClientConfig` objects used by the NATS transport
//! and the TCP request-plane.

use std::{path::Path, sync::Arc};

use anyhow::{Context, Result};
use rustls::{ClientConfig, RootCertStore, ServerConfig};
use rustls_pemfile::{certs, private_key};

/// Build a rustls `ServerConfig` from PEM certificate and key files.
///
/// - `client_ca_cert_path`: when `Some`, the server requires clients to
///   present a certificate signed by that CA (mutual TLS). When `None`,
///   client certificates are not requested.
pub fn server_tls_config(
    cert_path: &Path,
    key_path: &Path,
    client_ca_cert_path: Option<&Path>,
) -> Result<ServerConfig> {
    let cert_pem = std::fs::read(cert_path)
        .with_context(|| format!("reading cert: {}", cert_path.display()))?;
    let key_pem =
        std::fs::read(key_path).with_context(|| format!("reading key: {}", key_path.display()))?;

    let cert_chain = certs(&mut cert_pem.as_slice())
        .collect::<std::result::Result<Vec<_>, _>>()
        .context("parsing certificate PEM")?;

    let key = private_key(&mut key_pem.as_slice())
        .context("parsing private key PEM")?
        .context("no private key found in PEM")?;

    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let builder = ServerConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .context("configuring TLS protocol versions")?;

    let config = if let Some(ca_path) = client_ca_cert_path {
        let ca_pem = std::fs::read(ca_path)
            .with_context(|| format!("reading client CA cert: {}", ca_path.display()))?;
        let ca_certs = certs(&mut ca_pem.as_slice())
            .collect::<std::result::Result<Vec<_>, _>>()
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
            .with_single_cert(cert_chain, key)
            .context("building mTLS ServerConfig")?
    } else {
        builder
            .with_no_client_auth()
            .with_single_cert(cert_chain, key)
            .context("building ServerConfig")?
    };

    Ok(config)
}

/// Build a rustls `ClientConfig` for outbound TLS connections.
///
/// - `ca_cert_path`: trust this CA for verifying the server certificate.
///   When `None`, the root store is empty — supply a CA cert or use `insecure`.
/// - `insecure`: skip certificate verification entirely. **Dev/test only.**
/// - `client_cert_path` + `client_key_path`: when both are `Some`, the client
///   presents this certificate to the server (mutual TLS).
pub fn client_tls_config(
    ca_cert_path: Option<&Path>,
    insecure: bool,
    client_cert_path: Option<&Path>,
    client_key_path: Option<&Path>,
) -> Result<ClientConfig> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());

    if insecure {
        let builder = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .context("configuring TLS protocol versions")?
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(NoVerifier));
        let config = match (client_cert_path, client_key_path) {
            (Some(cert_path), Some(key_path)) => {
                let cert_pem = std::fs::read(cert_path)
                    .with_context(|| format!("reading client cert: {}", cert_path.display()))?;
                let key_pem = std::fs::read(key_path)
                    .with_context(|| format!("reading client key: {}", key_path.display()))?;
                let cert_chain = certs(&mut cert_pem.as_slice())
                    .collect::<std::result::Result<Vec<_>, _>>()
                    .context("parsing client certificate PEM")?;
                let key = private_key(&mut key_pem.as_slice())
                    .context("parsing client private key PEM")?
                    .context("no private key found in client PEM")?;
                builder
                    .with_client_auth_cert(cert_chain, key)
                    .context("building insecure mTLS ClientConfig")?
            }
            (None, None) => builder.with_no_client_auth(),
            _ => anyhow::bail!(
                "client_cert_path and client_key_path must both be set or both be unset"
            ),
        };
        return Ok(config);
    }

    let mut root_store = RootCertStore::empty();
    if let Some(ca_path) = ca_cert_path {
        let ca_pem = std::fs::read(ca_path)
            .with_context(|| format!("reading CA cert: {}", ca_path.display()))?;
        let ca_certs = certs(&mut ca_pem.as_slice())
            .collect::<std::result::Result<Vec<_>, _>>()
            .context("parsing CA certificate PEM")?;
        for cert in ca_certs {
            root_store
                .add(cert)
                .context("adding CA certificate to root store")?;
        }
    }
    // When no CA cert is provided, the root store is empty — the caller must
    // supply a CA cert or use `insecure = true`. This is intentional: in
    // cluster deployments, certs are issued by an internal CA and system roots
    // are not relevant.

    let builder = ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .context("configuring TLS protocol versions")?
        .with_root_certificates(root_store);

    let config = match (client_cert_path, client_key_path) {
        (Some(cert_path), Some(key_path)) => {
            let cert_pem = std::fs::read(cert_path)
                .with_context(|| format!("reading client cert: {}", cert_path.display()))?;
            let key_pem = std::fs::read(key_path)
                .with_context(|| format!("reading client key: {}", key_path.display()))?;
            let cert_chain = certs(&mut cert_pem.as_slice())
                .collect::<std::result::Result<Vec<_>, _>>()
                .context("parsing client certificate PEM")?;
            let key = private_key(&mut key_pem.as_slice())
                .context("parsing client private key PEM")?
                .context("no private key found in client PEM")?;
            builder
                .with_client_auth_cert(cert_chain, key)
                .context("building mTLS ClientConfig")?
        }
        (None, None) => builder.with_no_client_auth(),
        _ => {
            anyhow::bail!("client_cert_path and client_key_path must both be set or both be unset")
        }
    };

    Ok(config)
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
