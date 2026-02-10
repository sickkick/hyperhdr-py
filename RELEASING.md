# Releasing to PyPI

Steps to publish a new release to PyPI via GitHub Actions:

1. Create a PyPI API token
   - Log in to https://pypi.org, go to "Account settings" → "API tokens", and create a token scoped to the project or to the entire account.

2. Add the token to GitHub
   - In your repository, go to Settings → Secrets and variables → Actions → New repository secret.
   - Name the secret `PYPI_API_TOKEN` and paste the token value.

3. Create a release tag and push
   - Locally, bump the version in `pyproject.toml` (or use your release tooling), then create an annotated tag:

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

4. GitHub Actions will run the `Publish Python package` workflow when the tag is pushed and upload the built distributions to PyPI.

Local alternative (using `build` and `twine`):

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine upload dist/* -u __token__ -p $PYPI_API_TOKEN
```
