# Security

## Credentials

`tpdc-fetch` does not store credentials.

- **Policy A (open data):** the FTP credentials are returned per dataset by TPDC
  and used only in memory for the current download.
- **Policy B (login):** pass the browser cookie via `--cookie` or the
  `TPDC_COOKIE` environment variable. Do not commit it, paste it into issues, or
  save it in a shell history you share.
- **Policy C (application):** the tool does not bypass the application process.

## Repository hygiene

Before committing:

```bash
grep -RIn -e "download_" -e "password" -e "SESSION=" -e "token" .
```

The repository must not contain:

- cookies or session tokens,
- FTP usernames/passwords,
- GitHub tokens,
- personal absolute paths.

## Reporting

If you find a leaked credential or a security issue, open a private security
advisory on GitHub or contact the maintainer. Do not open a public issue with
the secret.
