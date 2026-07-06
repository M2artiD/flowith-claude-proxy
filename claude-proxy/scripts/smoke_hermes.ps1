param(
    [string]$BaseUrl = $env:HERMES_BASE_URL,
    [string]$ApiKey = $env:HERMES_API_KEY,
    [string]$Model = $env:HERMES_MODEL,
    [switch]$SyntaxOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = "http://127.0.0.1:8789" }
if ([string]::IsNullOrWhiteSpace($Model)) { $Model = "claude-5-sonnet" }
if ($SyntaxOnly) {
    Write-Host "Hermes smoke script syntax/options OK"
    exit 0
}

function Get-ConfiguredFlowithApiKey {
    $scriptDir = Split-Path -Parent $PSCommandPath
    $repoRoot = Split-Path -Parent $scriptDir
    $searchRoots = @($repoRoot, (Get-Location).Path) | Select-Object -Unique

    foreach ($root in $searchRoots) {
        $path = Join-Path $root ".flowith_api_key"
        if (Test-Path -LiteralPath $path) {
            $value = (Get-Content -LiteralPath $path -Raw).Trim().Trim('"').Trim("'")
            if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
        }
    }

    foreach ($root in $searchRoots) {
        $path = Join-Path $root ".env"
        if (-not (Test-Path -LiteralPath $path)) { continue }
        foreach ($line in Get-Content -LiteralPath $path) {
            $trimmed = $line.Trim()
            if (-not $trimmed.StartsWith("FLOWITH_API_KEY=")) { continue }
            $value = $trimmed.Substring("FLOWITH_API_KEY=".Length).Trim().Trim('"').Trim("'")
            if (-not [string]::IsNullOrWhiteSpace($value)) { return $value }
        }
    }

    return $null
}

if ([string]::IsNullOrWhiteSpace($ApiKey)) { $ApiKey = Get-ConfiguredFlowithApiKey }
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "No Hermes API key supplied. Set HERMES_API_KEY, FLOWITH_API_KEY in .env, or .flowith_api_key."
}
$BaseUrl = $BaseUrl.TrimEnd('/')
$Headers = @{ Authorization = "Bearer $ApiKey" }

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Invoke-JsonPost([string]$Path, [hashtable]$Body) {
    $json = $Body | ConvertTo-Json -Depth 20
    Invoke-RestMethod -Method Post -Uri "$BaseUrl$Path" -Headers $Headers -ContentType "application/json" -Body $json -UseBasicParsing
}

Write-Host "[1/7] GET /health"
$health = Invoke-RestMethod -Method Get -Uri "$BaseUrl/health" -Headers $Headers -UseBasicParsing
Assert-True ($null -ne $health) "health returned no body"

Write-Host "[2/7] GET /v1/models"
$models = Invoke-RestMethod -Method Get -Uri "$BaseUrl/v1/models" -Headers $Headers -UseBasicParsing
Assert-True ($null -ne $models.data) "models response missing data"

Write-Host "[3/7] non-streaming chat"
$chat = Invoke-JsonPost "/v1/chat/completions" @{
    model = $Model
    messages = @(@{ role = "user"; content = "Reply with exactly: hermes-smoke-ok" })
    stream = $false
    max_tokens = 32
}
$text = [string]$chat.choices[0].message.content
Assert-True ($text.Length -gt 0) "non-streaming response was empty"
Assert-True (-not ($text -match "<tool|</tool|<think|</think")) "non-streaming response leaked XML/think tags: $text"

Write-Host "[4/7] streaming chat"
$streamBody = @{
    model = $Model
    messages = @(@{ role = "user"; content = "Say hello in one short sentence." })
    stream = $true
    max_tokens = 64
} | ConvertTo-Json -Depth 20
$response = Invoke-WebRequest -Method Post -Uri "$BaseUrl/v1/chat/completions" -Headers $Headers -ContentType "application/json" -Body $streamBody -UseBasicParsing
$raw = [string]$response.Content
Assert-True ($raw -match "data:") "streaming response had no SSE data lines"
Assert-True ($raw -match '"delta"') "streaming response had no delta objects"
Assert-True (-not ($raw -match "<tool|</tool|<think|</think")) "streaming response leaked XML/think tags"

Write-Host "[5/7] tool-call request"
$toolResp = Invoke-JsonPost "/v1/chat/completions" @{
    model = $Model
    messages = @(@{ role = "user"; content = "Use the echo tool with text hermes-tool-ok." })
    tools = @(@{
        type = "function"
        function = @{
            name = "echo"
            description = "Echo input text."
            parameters = @{
                type = "object"
                properties = @{ text = @{ type = "string" } }
                required = @("text")
            }
        }
    })
    tool_choice = "auto"
    stream = $false
    max_tokens = 128
}
$toolJson = $toolResp | ConvertTo-Json -Depth 30
Assert-True (-not ($toolJson -match "<tool|</tool|<tool_call|</tool_call")) "tool response leaked raw XML"

Write-Host "[6/7] duplicate final text heuristic"
$combinedText = ($raw -split "`n" | Where-Object { $_ -like "data:*" }) -join "`n"
Assert-True (-not ($combinedText -match "(.{12,})\1\s*$")) "streaming output appears to duplicate final text"

Write-Host "[7/7] partial think-tag tail flush heuristic"
$tail = Invoke-JsonPost "/v1/chat/completions" @{
    model = $Model
    messages = @(@{ role = "user"; content = "Reply with visible text only: angle bracket test." })
    stream = $false
    max_tokens = 64
}
$tailText = [string]$tail.choices[0].message.content
Assert-True ($tailText.Length -gt 0) "partial think-tag heuristic response was empty"
Assert-True (-not ($tailText -match "<think|</think")) "think tag leaked in tail heuristic"

Write-Host "Hermes smoke checks passed for $BaseUrl"
