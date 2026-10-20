<#
.SYNOPSIS
    DeepSeek Agent Desktop 的 UI Automation 驱动工具（端到端测试用）。

.DESCRIPTION
    通过 Windows UI Automation 读写桌面应用的可访问性树。
    所有输出写 UTF-8 文件，避免 PowerShell 5.1 的 GBK 默认解码把中文变乱码。

    用法：
      ui.ps1 dump                      列出窗口内全部元素（含坐标）
      ui.ps1 text                      只列 Text 元素，按 Y 坐标排序（读界面状态）
      ui.ps1 set   <name> <value>      给控件设值（ValuePattern）
      ui.ps1 click <name>              点击控件（InvokePattern）
      ui.ps1 find  <name>              定位控件，打印类型与坐标
      ui.ps1 focus <name>              把焦点移到控件（SetFocus）
      ui.ps1 key   <键名> [修饰键]     发按键（如 "Enter"、"OemPlus" "Control"）

    匹配规则：AutomationId 精确 > Name 精确 > Name 包含。
    多个命中时报错并列出候选，绝不自动挑第一个 —— 点错按钮的代价比失败大。

.NOTES
    进程按窗口标题定位，默认匹配 "DeepSeek Agent Desktop"。
#>
# 参数一律从环境变量读取。
# 原因：本机 PowerShell 5.1 的 ExecutionPolicy 模块不可加载（无法 -ExecutionPolicy Bypass），
# 只能走 `powershell -Command "<脚本文本>"` 内联执行；而内联模式下 -Command 之后的所有
# 内容都被当成命令文本，param 块拿不到命令行参数。环境变量在两种模式下都可用。
param(
    [string]$Action = $env:UIA_ACTION,
    [string]$Target = $env:UIA_TARGET,
    [string]$Value  = $env:UIA_VALUE,
    [string]$ProcessName = $(if ($env:UIA_PROCESS) { $env:UIA_PROCESS } else { 'DeepSeekAgent.Desktop' }),
    [string]$OutFile = $(if ($env:UIA_OUT) { $env:UIA_OUT } else { "$env:TEMP\uia_out.txt" })
)

if (-not $Action) { throw "缺少 UIA_ACTION 环境变量（dump / text / set / click / find / focus / key）" }
if ($Action -notin @('dump', 'text', 'set', 'click', 'find', 'focus', 'key')) { throw "未知 action: $Action" }

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$script:sb = New-Object System.Text.StringBuilder

function Write-Line([string]$s) { [void]$script:sb.AppendLine($s) }

function Save-Out {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($OutFile, $script:sb.ToString(), $enc)
}

function Get-AppWindow {
    $procs = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { throw "进程 $ProcessName 未运行" }

    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $found = @()
    foreach ($p in $procs) {
        $cond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $p.Id)
        $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
        foreach ($w in $wins) { $found += $w }
    }
    # 主窗口特征：ClassName 为 Window 且 Name 非空
    $main = $found | Where-Object { $_.Current.ClassName -eq 'Window' -and $_.Current.Name } | Select-Object -First 1
    if (-not $main) { throw "找不到主窗口（进程在跑但没有可见 Window）" }
    return $main
}

# 返回所有「可识别」的元素：有 AutomationId 或有 Name 的。
# WPF 把 x:Name 映射到 AutomationId，而 Name 是显示文本 ——
# 前者稳定（改文案不影响），后者易变。定位一律优先 AutomationId。
function Get-NamedElements($win) {
    $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                        [System.Windows.Automation.Condition]::TrueCondition)
    $list = @()
    foreach ($e in $all) {
        try {
            if ([string]::IsNullOrWhiteSpace($e.Current.Name) -and
                [string]::IsNullOrWhiteSpace($e.Current.AutomationId)) { continue }
            $list += $e
        } catch { }
    }
    return $list
}

# 统一匹配：先按 AutomationId 精确，再按 Name 精确，最后按 Name 包含。
# 返回 @{ Hits = <数组>; Mode = <'aid'|'name'|'substring'|'none'> }
function Resolve-Element($elems, [string]$target) {
    if (-not $target) { return @{ Hits = @(); Mode = 'none' } }
    $byAid = @($elems | Where-Object { $_.Current.AutomationId -eq $target })
    if ($byAid.Count -gt 0) { return @{ Hits = $byAid; Mode = 'aid' } }
    $byName = @($elems | Where-Object { $_.Current.Name -eq $target })
    if ($byName.Count -gt 0) { return @{ Hits = $byName; Mode = 'name' } }
    $bySub = @($elems | Where-Object { $_.Current.Name -like "*$target*" })
    return @{ Hits = $bySub; Mode = $(if ($bySub.Count -gt 0) { 'substring' } else { 'none' }) }
}

function Format-Elem($e, [int]$idx) {
    $ct = $e.Current.ControlType.ProgrammaticName -replace 'ControlType\.', ''
    $r = $e.Current.BoundingRectangle
    $rect = "({0},{1} {2}x{3})" -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height
    return ("#{0,-3} {1,-12} {2} | {3}" -f $idx, $ct, $rect, $e.Current.Name)
}

# ---- 主流程 ----
try {
    $win = Get-AppWindow

    switch ($Action) {
        'dump' {
            Write-Line ("WINDOW: " + $win.Current.Name)
            Write-Line ("RECT: " + $win.Current.BoundingRectangle.ToString())
            Write-Line ""
            $elems = Get-NamedElements $win
            Write-Line ("ELEMENT COUNT: " + $elems.Count)
            Write-Line ""
            for ($i = 0; $i -lt $elems.Count; $i++) {
                Write-Line (Format-Elem $elems[$i] ($i + 1))
            }
        }

        'text' {
            $elems = Get-NamedElements $win | Where-Object {
                $_.Current.ControlType.ProgrammaticName -eq 'ControlType.Text'
            }
            # 按 Y 再按 X 排序，还原视觉上的阅读顺序
            $sorted = $elems | Sort-Object @{E = { [int]$_.Current.BoundingRectangle.Y } },
                                          @{E = { [int]$_.Current.BoundingRectangle.X } }
            foreach ($e in $sorted) { Write-Line $e.Current.Name }
        }

        'find' {
            if (-not $Target) { throw "find 需要 <AutomationId 或 Name>" }
            $elems = Get-NamedElements $win
            $res = Resolve-Element $elems $Target
            if ($res.Hits.Count -eq 0) { Write-Line "NOT FOUND: $Target" }
            else {
                Write-Line ("MATCHES: " + $res.Hits.Count + " (by " + $res.Mode + ")")
                for ($i = 0; $i -lt $res.Hits.Count; $i++) { Write-Line (Format-Elem $res.Hits[$i] ($i + 1)) }
            }
        }

        'set' {
            if (-not $Target) { throw "set 需要 <AutomationId 或 Name> <value>" }
            $elems = Get-NamedElements $win
            $res = Resolve-Element $elems $Target
            $hits = $res.Hits
            if ($hits.Count -eq 0) { throw "找不到控件: $Target" }
            # 同 click：优先可编辑控件
            if ($hits.Count -gt 1) {
                $editable = @($hits | Where-Object {
                    $ct = $_.Current.ControlType.ProgrammaticName
                    $ct -in @('ControlType.Edit', 'ControlType.ComboBox', 'ControlType.Document')
                })
                if ($editable.Count -ge 1) { $hits = $editable }
            }
            if ($hits.Count -gt 1) {
                Write-Line "AMBIGUOUS: 命中 $($hits.Count) 个，拒绝执行"
                for ($i = 0; $i -lt $hits.Count; $i++) { Write-Line (Format-Elem $hits[$i] ($i + 1)) }
                break
            }
            $e = $hits[0]
            $vp = $null
            if (-not $e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
                throw "控件不支持 ValuePattern（类型: $($e.Current.ControlType.ProgrammaticName)）"
            }
            $vp.SetValue($Value)
            Write-Line ("SET OK -> " + $e.Current.ControlType.ProgrammaticName + " : " + $Value)
        }

        'click' {
            if (-not $Target) { throw "click 需要 <AutomationId 或 Name>" }
            $elems = Get-NamedElements $win
            $res = Resolve-Element $elems $Target
            $hits = $res.Hits
            if ($hits.Count -eq 0) { throw "找不到控件: $Target" }
            # 按钮的模板内部往往还有一个同名 TextBlock 在画标签。
            # 优先保留真正可交互的控件（Button / ListItem / CheckBox / MenuItem），
            # 否则每次点按钮都会撞上「命中 2 个」。
            if ($hits.Count -gt 1) {
                $actionable = @($hits | Where-Object {
                    $ct = $_.Current.ControlType.ProgrammaticName
                    $ct -in @('ControlType.Button', 'ControlType.ListItem',
                              'ControlType.CheckBox', 'ControlType.MenuItem',
                              'ControlType.RadioButton', 'ControlType.TabItem')
                })
                if ($actionable.Count -ge 1) { $hits = $actionable }
            }
            if ($hits.Count -gt 1) {
                Write-Line "AMBIGUOUS: 命中 $($hits.Count) 个，拒绝执行"
                for ($i = 0; $i -lt $hits.Count; $i++) { Write-Line (Format-Elem $hits[$i] ($i + 1)) }
                break
            }
            $e = $hits[0]
            $ip = $null
            if (-not $e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
                throw "控件不支持 InvokePattern（类型: $($e.Current.ControlType.ProgrammaticName)）"
            }
            $ip.Invoke()
            Write-Line ("CLICK OK -> " + $e.Current.Name)
        }

        'focus' {
            if (-not $Target) { throw "focus 需要 <AutomationId 或 Name>" }
            $elems = Get-NamedElements $win
            $res = Resolve-Element $elems $Target
            if ($res.Hits.Count -eq 0) { throw "找不到控件: $Target" }
            if ($res.Hits.Count -gt 1) {
                Write-Line "AMBIGUOUS: 命中 $($res.Hits.Count) 个，拒绝执行"
                for ($i = 0; $i -lt $res.Hits.Count; $i++) { Write-Line (Format-Elem $res.Hits[$i] ($i + 1)) }
                break
            }
            $res.Hits[0].SetFocus()
            Start-Sleep -Milliseconds 150
            $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
            Write-Line ("FOCUS OK -> aid='" + $focused.Current.AutomationId + "' name='" + $focused.Current.Name + "'")
        }

        'key' {
            # keybd_event 把键发给**当前前台窗口**，所以调用前必须让目标窗口在前台。
            # 但本机 Windows 的前台锁定会拒绝 SetForegroundWindow（ERROR_PRIVILEGE_NOT_HELD）。
            # 约定：调用方先用 `focus <控件>` 把焦点落到目标窗口的控件上 —— 那一步成功
            # 就说明窗口已经在前台，这里不再重复激活。
            if (-not $Target) { throw "key 需要 <键名> [修饰键]" }

            $mods = @()
            if ($Value) { $mods = $Value -split ',' | ForEach-Object { $_.Trim() } }

            # SendKeys 发给前台窗口。UIA 的 SetFocus 只把焦点放到控件，
            # 不保证窗口在前台（终端可能仍占着）。用 Shell 的 AppActivate 提上来 ——
            # 它走的是 shell 激活接口，不受 SetForegroundWindow 的前台锁限制。
            # 顺序很关键：AppActivate 会重置窗口内的焦点，所以必须**先激活窗口、
            # 再把焦点落到目标控件**，否则按键会打在窗口的空处。
            $procId = $win.Current.ProcessId
            $shell = New-Object -ComObject WScript.Shell
            [void]$shell.AppActivate($procId)
            Start-Sleep -Milliseconds 250

            if ($env:UIA_FOCUS) {
                $felems = Get-NamedElements $win
                $fres = Resolve-Element $felems $env:UIA_FOCUS
                if ($fres.Hits.Count -ge 1) {
                    $fres.Hits[0].SetFocus()
                    Start-Sleep -Milliseconds 150
                    Write-Line ("REFOCUS -> " + $env:UIA_FOCUS)
                }
            }

            # 为什么不用 keybd_event：那需要 Add-Type -MemberDefinition 在运行时编译 C#，
            # 而本机沙箱拒绝启动编译器（报「A required privilege is not held by the client」，
            # 字面像前台权限问题，实际是编译权限）。
            # SendKeys 是纯托管实现，不需要编译，且支持 ^（Ctrl）/ +（Shift）/ %（Alt）前缀。
            Add-Type -AssemblyName System.Windows.Forms

            # WPF 键名 → SendKeys 写法
            $keyMap = @{
                'OemPlus'  = '=';  'Add'      = '{ADD}'
                'OemMinus' = '-';  'Subtract' = '{SUBTRACT}'
                'D0'       = '0';  'NumPad0'  = '{NUM0}'
                'Enter'    = '{ENTER}'; 'Return' = '{ENTER}'
                'Escape'   = '{ESC}';   'Tab'    = '{TAB}'
                'Back'     = '{BACKSPACE}'; 'Space' = ' '
                'Up'       = '{UP}';    'Down'   = '{DOWN}'
                'Left'     = '{LEFT}';  'Right'  = '{RIGHT}'
                'Home'     = '{HOME}';  'End'    = '{END}'
                'Delete'   = '{DELETE}'; 'Insert' = '{INSERT}'
            }

            $prefix = ''
            foreach ($m in $mods) {
                switch ($m) {
                    'Control' { $prefix += '^' }
                    'Shift'   { $prefix += '+' }
                    'Alt'     { $prefix += '%' }
                    default   { throw "未知修饰键: $m（支持 Control / Shift / Alt）" }
                }
            }

            $keyText = if ($keyMap.ContainsKey($Target)) { $keyMap[$Target] }
                       elseif ($Target -match '^[A-Za-z0-9]$') { $Target }
                       else { throw "未知键名: $Target（可用: $($keyMap.Keys -join ', ') 或单字母/单数字）" }

            [System.Windows.Forms.SendKeys]::SendWait($prefix + $keyText)
            Write-Line ("KEY OK -> " + (($mods -join '+') + $(if ($mods.Count -gt 0) { '+' } else { '' }) + $Target))
        }
    }

    Save-Out
    exit 0
}
catch {
    Write-Line ("ERROR: " + $_.Exception.Message)
    Save-Out
    exit 1
}
