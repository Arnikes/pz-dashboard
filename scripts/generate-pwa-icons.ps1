# Reproduce the existing favicon geometry as antialiased, opaque PNG app icons.
Add-Type -AssemblyName System.Drawing
$iconDirectory = Join-Path $PSScriptRoot '..\dashboard\static\icons'
New-Item -ItemType Directory -Path $iconDirectory -Force | Out-Null
foreach ($spec in @(@('icon-192.png', 192, 24), @('icon-512.png', 512, 24), @('maskable-512.png', 512, 32), @('apple-touch-icon.png', 180, 28))) {
    $size = [int]$spec[1]
    $canvas = [single]$spec[2]
    $bitmap = [System.Drawing.Bitmap]::new($size, $size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.Clear([System.Drawing.ColorTranslator]::FromHtml('#0d0b09'))
    $scale = $size / $canvas
    $graphics.ScaleTransform($scale, $scale)
    $offset = ($canvas - 24) / 2
    $graphics.TranslateTransform($offset, $offset)
    $pen = [System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml('#b3352b'), 1.6)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $brush = [System.Drawing.SolidBrush]::new($pen.Color)
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $path.AddBezier(12, 4.4, 8.355, 4.4, 5.4, 7.355, 5.4, 11)
    $path.AddBezier(5.4, 11, 5.4, 13.5, 6.7, 15.5, 8.7, 16.6)
    $path.AddLine(8.7, 16.6, 8.7, 19.2)
    $path.AddBezier(8.7, 19.2, 8.7, 19.973, 9.327, 20.6, 10.1, 20.6)
    $path.AddLine(10.1, 20.6, 13.9, 20.6)
    $path.AddBezier(13.9, 20.6, 14.673, 20.6, 15.3, 19.973, 15.3, 19.2)
    $path.AddLine(15.3, 19.2, 15.3, 16.6)
    $path.AddBezier(15.3, 16.6, 17.3, 15.5, 18.6, 13.5, 18.6, 11)
    $path.AddBezier(18.6, 11, 18.6, 7.355, 15.645, 4.4, 12, 4.4)
    $path.CloseFigure()
    $graphics.DrawPath($pen, $path)
    $graphics.FillEllipse($brush, 8, 10, 2.8, 2.8)
    $graphics.FillEllipse($brush, 13.2, 10, 2.8, 2.8)
    $graphics.DrawLine($pen, 10.4, 15.6, 13.6, 15.6)
    $bitmap.Save((Join-Path $iconDirectory $spec[0]), [System.Drawing.Imaging.ImageFormat]::Png)
    $path.Dispose()
    $brush.Dispose()
    $pen.Dispose()
    $graphics.Dispose()
    $bitmap.Dispose()
}
