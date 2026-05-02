# AdLens

AdLens crawls webpages, detects ads, checks Acceptable Ads compliance, and generates filter rules.

The package entrypoint is:

- python -m adlens

## Requirements

- Python 3.10+
- pip
- Chromium browser binaries for Playwright

## Install

From the repository root:

```bash
cd /your/path/adlens
```

If you use conda, activate your environment first:

```bash
conda activate adcompliance
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Install Playwright browser binaries:

```bash
python -m playwright install chromium
```

## Verify startup

```bash
python -m adlens --help
```

## Usage

### 1) Single URL (heuristic mode)

```bash
python -m adlens --url https://example.com --mode heuristic
```

### 2) Single URL (LLM mode)

```bash
python -m adlens --url https://example.com --mode llm --api-key YOUR_OPENAI_KEY
```

You can also set the environment variable:

```bash
export OPENAI_API_KEY=YOUR_OPENAI_KEY
python -m adlens --url https://example.com --mode llm
```

### 3) Batch scan from file

Create a text file with one URL per line, then run:

```bash
python -m adlens --urls websites.txt --mode heuristic
```

### 4) Parallel batch scan

```bash
python -m adlens --urls websites.txt --mode heuristic --workers 4 --worker-timeout 300
```

## Useful options

- --viewport desktop|mobile|tablet
- --timeout 45
- --scan-timeout 180
- --skip-existing
- --no-video
- --no-headless
- --output-dir /path/to/output
- --exception-list /path/to/exceptionlist.txt

## Output structure

Default output directory:

- adlens_results/

Per-domain session output:

- adlens_results/<domain>/session_<timestamp>/

Typical artifacts:

- compliance_report.json
- rules.txt (if rules were generated)
- network_log.json
- full_page.png (or viewport.png fallback)
- ads/ (element screenshots)
- session_recording.webm (unless --no-video)

When running in parallel, additional files are written in the output root:

- logs/*.log
- progress.jsonl
- parallel_summary.json

## Troubleshooting

### ModuleNotFoundError: No module named playwright

Install dependencies and Playwright browser binaries:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### LLM mode says API key is required

Set one of:

- --api-key YOUR_OPENAI_KEY
- OPENAI_API_KEY environment variable

### Want to avoid re-scanning completed sites

Use:

```bash
python -m adlens --urls websites.txt --mode heuristic --skip-existing
```
