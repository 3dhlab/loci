# Security

Send vulnerability reports to Craig Stevens at craig.stevens@austin.utexas.edu. Include the affected version, the potential impact, steps to reproduce the issue with sample content, and relevant logs with sensitive information removed. Keep passwords, access tokens and participant records private.

Use [GitHub's private vulnerability reporting](https://github.com/3dhlab/loci/security/advisories/new) or the email address above for security reports. Use [GitHub Issues](https://github.com/3dhlab/loci/issues) for ordinary bugs and support questions.

Security verification covers the documented local demonstration and its required GitHub Actions checks, including authentication, public/private projection and disposable database/media services. A hosted installation needs a separate review of its authentication, network access, secrets and operating procedures.

This source pins brace-expansion 5.0.12 and PyJWT 2.15.0. Earlier v0.1.0 and v0.1.1 archives retain their previous dependency versions; use the [release notes](https://github.com/3dhlab/loci/releases) and the manifests in your installed checkout to identify the applicable fixes. Updating the source alone does not rebuild an existing installation. Rebuild its API and web images using the documented setup, preserve its database/media volumes, and follow the [explicit generated-demo refresh](examples/README.md#refresh-the-generated-sample-in-an-existing-installation) only for an eligible synthetic installation.

Dependency audits describe the versions and advisories checked at a particular time; repeat them as the software changes. These checks do not establish whole-system security or production-hosting readiness.
