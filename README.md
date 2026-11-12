# AI-Projects

This repository contains the runnable VOIS TransformSQL project in `VOIS_TransformSQL_Engine (1).zip.txt`.

## Run locally

From the repository root, run:

```powershell
.\run.ps1
```

The script extracts the project into `VOIS_TransformSQL_Engine` when needed and starts the combined backend/frontend server on port `8080`.

Open `http://localhost:8080` and use the local demo login shown on the sign-in screen.

SQL generation, review, copying, and downloads work without BigQuery credentials. BigQuery execution and comparison require credentials available to the backend host.
