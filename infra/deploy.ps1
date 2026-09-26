# Redeploy the Lambda after code changes.
# Builds a Linux-compatible dependency bundle + app code, zips, and updates the function.
# Run from the project root:  .\infra\deploy.ps1

$ErrorActionPreference = "Stop"
$region = "us-east-1"
$fn = "freshdesk-refund-agent"

Write-Host "1/3 Installing Linux deps into build/ ..."
if (Test-Path build) { Remove-Item -Recurse -Force build }
New-Item -ItemType Directory build | Out-Null
pip install fastapi mangum "mcp==1.28.1" python-dotenv `
  --platform manylinux2014_x86_64 --python-version 3.13 --implementation cp `
  --only-binary=:all: --target build --no-deps
pip install pydantic pydantic-settings "httpx>=0.27" httpx-sse starlette anyio sse-starlette `
  "python-multipart" jsonschema pydantic-core typing-extensions annotated-types annotated-doc `
  certifi idna h11 httpcore click referencing jsonschema-specifications rpds-py sniffio typing-inspection `
  --platform manylinux2014_x86_64 --python-version 3.13 --implementation cp --only-binary=:all: --target build

Write-Host "2/3 Bundling app code + zipping ..."
Copy-Item src\webhook_server.py, src\mcp_client.py, src\policy.py, src\ai.py build\ -Force
if (Test-Path function.zip) { Remove-Item function.zip }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory("$PWD\build", "$PWD\function.zip")

Write-Host "3/3 Updating Lambda code ..."
aws lambda update-function-code --function-name $fn --zip-file fileb://function.zip --region $region | Out-Null
aws lambda wait function-updated --function-name $fn --region $region
Write-Host "Deployed."
