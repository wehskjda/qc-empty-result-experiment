$ErrorActionPreference = 'Stop'
$reportRoot = $PSScriptRoot
$wordPath = Join-Path $reportRoot '实验报告初稿.docx'
$pdfPath = Join-Path $reportRoot '实验报告初稿.pdf'
$office = $null
$document = $null
$previousAlerts = $null
$existingDocuments = 0
try {
    $office = New-Object -ComObject kwps.Application
    $existingDocuments = $office.Documents.Count
    $previousAlerts = $office.DisplayAlerts
    if ($existingDocuments -eq 0) { $office.Visible = $false }
    $office.DisplayAlerts = 0
    $document = $office.Documents.Open($wordPath, $false, $true)
    $document.Repaginate()
    $document.ExportAsFixedFormat($pdfPath, 17)
    [pscustomobject]@{
        renderer = 'WPS native document layout via COM'
        input = $wordPath
        output = $pdfPath
        pageCount = $document.ComputeStatistics(2)
    } | ConvertTo-Json
}
finally {
    if ($null -ne $document) { $document.Close(0); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($document) }
    if ($null -ne $office) {
        if ($null -ne $previousAlerts) { $office.DisplayAlerts = $previousAlerts }
        if ($existingDocuments -eq 0 -and $office.Documents.Count -eq 0) { $office.Quit() }
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($office)
    }
}
