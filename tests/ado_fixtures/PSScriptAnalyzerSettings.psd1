# tests/ado_fixtures/PSScriptAnalyzerSettings.psd1 - PSScriptAnalyzer rules for Windows PowerShell 5.1 Desktop compatibility
# Pure ASCII, LF line endings, BOM-less UTF-8 (AC-10, TP-10).

@{
    IncludeRules = @(
        'PSUseCompatibleSyntax',
        'PSUseCompatibleCmdlets',
        'PSUseCompatibleCommands',
        'PSUseCompatibleTypes'
    )

    Rules = @{
        PSUseCompatibleSyntax = @{
            Enable = $true
            TargetVersions = @('5.1')
        }

        PSUseCompatibleCmdlets = @{
            compatibility = @('desktop-5.1.14393.206-windows')
        }

        PSUseCompatibleCommands = @{
            Enable = $true
            TargetProfiles = @(
                'win-48_x64_10.0.17763.0_5.1.17763.316_x64_4.0.30319.42000_framework',
                'win-8_x64_10.0.14393.0_5.1.14393.2791_x64_4.0.30319.42000_framework'
            )
        }

        PSUseCompatibleTypes = @{
            Enable = $true
            TargetProfiles = @(
                'win-48_x64_10.0.17763.0_5.1.17763.316_x64_4.0.30319.42000_framework',
                'win-8_x64_10.0.14393.0_5.1.14393.2791_x64_4.0.30319.42000_framework'
            )
        }
    }
}
