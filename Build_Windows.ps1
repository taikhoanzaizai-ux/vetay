$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-Location $PSScriptRoot
$bundle = Join-Path $PWD 'bundle'
$runtime = Join-Path $bundle 'runtime'
$engine = Join-Path $bundle 'engine'
New-Item -ItemType Directory -Force $runtime,$engine | Out-Null
$embed = Join-Path $env:RUNNER_TEMP 'python-vetay.zip'
Invoke-WebRequest 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile $embed
Expand-Archive -LiteralPath $embed -DestinationPath $runtime -Force
@('python311.zip','.','..','Lib\site-packages','import site') |
    Set-Content -LiteralPath (Join-Path $runtime 'python311._pth') -Encoding ascii
$deps = Join-Path $runtime 'Lib\site-packages'
git clone --depth 1 https://github.com/letuandat131/whiteboard-animation-vi.git "$env:RUNNER_TEMP\vetay-engine"
$source = Join-Path $env:RUNNER_TEMP 'vetay-engine'
foreach($entry in @('handdraw','assets','vendor_runtime','scripts','LICENSE','THIRD_PARTY_NOTICES.md')){
    Copy-Item -LiteralPath (Join-Path $source $entry) -Destination $engine -Recurse
}
python -m pip install --target $deps torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cpu
python -m pip install --target $deps -r (Join-Path $source 'requirements.txt') -r requirements.txt
python (Join-Path $engine 'scripts\download_model.py')
$ffmpeg = Join-Path $env:RUNNER_TEMP 'vetay-ffmpeg.zip'
Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $ffmpeg
$expanded = Join-Path $env:RUNNER_TEMP 'vetay-ffmpeg'
Expand-Archive -LiteralPath $ffmpeg -DestinationPath $expanded
$ffmpegDir = Get-ChildItem -LiteralPath $expanded -Directory | Select-Object -First 1
Copy-Item -LiteralPath (Join-Path $ffmpegDir.FullName 'bin\ffmpeg.exe') -Destination $bundle
Copy-Item -LiteralPath (Join-Path $ffmpegDir.FullName 'LICENSE') -Destination (Join-Path $bundle 'FFmpeg-LICENSE.txt')
Copy-Item -LiteralPath app.py,README.md,version.txt -Destination $bundle
$iconPath = Join-Path $bundle 'VeTay.ico'
python -m pip install Pillow
python -c "from PIL import Image,ImageDraw; im=Image.new('RGBA',(256,256)); d=ImageDraw.Draw(im); d.rounded_rectangle((12,12,244,244),44,fill='#08795d'); d.line(((50,178),(90,135),(128,155),(200,62)),fill='white',width=18); im.save(r'$iconPath')"
& (Join-Path $runtime 'python.exe') -c "import torch, gradio, webview, faster_whisper, edge_tts, playwright.sync_api; print(torch.__version__)"
& (Join-Path $runtime 'python.exe') app.py --check
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" "/DAppVersion=$((Get-Content version.txt -Raw).Trim())" installer.iss
