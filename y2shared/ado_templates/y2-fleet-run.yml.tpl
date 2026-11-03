__HEADER_COMMENT__
parameters:
  - name: vmFilter
    type: object
    default: []
  - name: parallel
    type: number
    default: 0

stages:
  # ------------------------------------------------------------------ Validate
  - stage: Validate
    jobs:
      - job: Validate
        pool:
__POOL_YAML__
        steps:
          - checkout: self
          - template: y2-setup.yml
          - bash: |
              set -euo pipefail
              __TOOL__ validate "__JOB_REL__"
              __TOOL__ compile "__JOB_REL__" -o "$(Agent.TempDirectory)/payload_check"
              if ! diff -u "__PAYLOAD_REL__" "$(Agent.TempDirectory)/payload_check"; then
                echo "##vso[task.logissue type=error]Compiled payload differs from committed __PAYLOAD_REL__. Run: __TOOL__ export-ado ..."
                exit 1
              fi
              __TOOL__ explain "__JOB_REL__" --format text -o "$(Agent.TempDirectory)/explain_check.txt"
              if ! diff -u "__PLAN_TXT_REL__" "$(Agent.TempDirectory)/explain_check.txt"; then
                echo "##vso[task.logissue type=error]Explain text output differs from committed __PLAN_TXT_REL__. Run: __TOOL__ export-ado ..."
                exit 1
              fi
              __TOOL__ explain "__JOB_REL__" --format md -o "$(Agent.TempDirectory)/explain_check.md"
              if ! diff -u "__PLAN_MD_REL__" "$(Agent.TempDirectory)/explain_check.md"; then
                echo "##vso[task.logissue type=error]Explain markdown output differs from committed __PLAN_MD_REL__. Run: __TOOL__ export-ado ..."
                exit 1
              fi
              echo "##vso[task.uploadsummary]$(Agent.TempDirectory)/explain_check.md"
              mkdir -p "$(Build.ArtifactStagingDirectory)/y2-plan/payload"
              mkdir -p "$(Build.ArtifactStagingDirectory)/y2-plan/plan"
              cp "__PAYLOAD_REL__" "$(Build.ArtifactStagingDirectory)/y2-plan/payload/"
              cp "__PLAN_TXT_REL__" "__PLAN_MD_REL__" "$(Build.ArtifactStagingDirectory)/y2-plan/plan/"
            displayName: Validate job, payload and plan
          - publish: $(Build.ArtifactStagingDirectory)/y2-plan
            artifact: y2-plan

  # ------------------------------------------------------------------ Fleet
  - stage: Fleet
    dependsOn: Validate
    jobs:
      # ---------- Phase 1: run-command #1 (runCommand POST, no wait) ----------
      - deployment: Invoke
        environment: __ENVIRONMENT__
        pool:
__POOL_YAML__
        timeoutInMinutes: 120
        strategy:
          runOnce:
            deploy:
              steps:
                - download: none
                - checkout: self
                - template: y2-setup.yml
                - task: AzureCLI@2
                  name: fire
                  displayName: "Phase 1 · invoke (run-command 1 of 2)"
                  inputs:
                    azureSubscription: __SERVICE_CONNECTION__
                    scriptType: bash
                    scriptLocation: inlineScript
                    inlineScript: |
                      set -uo pipefail
                      set -f
                      args=()
                      for v in ${{ join(' ', parameters.vmFilter) }}; do
                        args+=(--vm "$v")
                      done
                      if [ "${{ parameters.parallel }}" -gt 0 ]; then
                        args+=(--parallel "${{ parameters.parallel }}")
                      fi
                      out="$(Agent.TempDirectory)/invoke.out"
                      __TOOL__ run "__JOB_REL__" --phase invoke -y "${args[@]}" | tee "$out"
                      rc=${PIPESTATUS[0]}
                      fired=0
                      while IFS= read -r line; do
                        case "$line" in
                          fired=*) fired="${line#fired=}" ;;
                        esac
                      done < "$out"
                      echo "##vso[task.setvariable variable=fired;isOutput=true]$fired"
                      exit "$rc"
                - bash: |
                    set -euo pipefail
                    if [ -d "__PARTS_REL__" ]; then
                      tar -czf "$(Build.ArtifactStagingDirectory)/y2-state.tgz" -C "$(Build.SourcesDirectory)" "__PARTS_REL__"
                      echo "##vso[task.setvariable variable=hasState;isOutput=true]true"
                    else
                      echo "##vso[task.setvariable variable=hasState;isOutput=true]false"
                    fi
                  name: pack
                  displayName: Pack run state
                  condition: always()
                - publish: $(Build.ArtifactStagingDirectory)/y2-state.tgz
                  artifact: y2-state-invoke
                  displayName: Publish run state (for Collect)
                  condition: and(always(), eq(variables['pack.hasState'], 'true'))

      # ---------- Wait: agentless, holds no agent ----------
      - job: Wait
        dependsOn: Invoke
        condition: and(succeeded(), ne(dependencies.Invoke.outputs['Invoke.fire.fired'], '0'))
        pool: server
        timeoutInMinutes: __WAIT_TIMEOUT__
        steps:
          - task: Delay@1
            displayName: "Wait ≥ sleep_before_collect"
            inputs:
              delayForMinutes: '__DELAY_MINUTES__'

      # ---------- Phase 2: run-command #2 (reader) + merge ----------
      - job: Collect
        dependsOn: [Invoke, Wait]
        condition: and(in(dependencies.Wait.result, 'Succeeded', 'Skipped'), or(eq(dependencies.Invoke.result, 'Succeeded'), and(eq(dependencies.Invoke.result, 'Failed'), eq(dependencies.Invoke.outputs['Invoke.pack.hasState'], 'true'))))
        pool:
__POOL_YAML__
        timeoutInMinutes: __COLLECT_TIMEOUT__
        steps:
          - checkout: self
          - template: y2-setup.yml
          - download: current
            artifact: y2-state-invoke
            condition: eq(dependencies.Invoke.outputs['Invoke.pack.hasState'], 'true')
          - bash: |
              set -euo pipefail
              if [ -f "$(Pipeline.Workspace)/y2-state-invoke/y2-state.tgz" ]; then
                tar -xzf "$(Pipeline.Workspace)/y2-state-invoke/y2-state.tgz" -C "$(Build.SourcesDirectory)"
              fi
            displayName: Restore run state
          - task: AzureCLI@2
            name: collect
            displayName: "Phase 2 · collect (run-command 2 of 2) + Phase 3 post + merge"
            inputs:
              azureSubscription: __SERVICE_CONNECTION__
              scriptType: bash
              scriptLocation: inlineScript
              inlineScript: |
                set -uo pipefail
                set -f
                args=()
                for v in ${{ join(' ', parameters.vmFilter) }}; do
                  args+=(--vm "$v")
                done
                if [ "${{ parameters.parallel }}" -gt 0 ]; then
                  args+=(--parallel "${{ parameters.parallel }}")
                fi
                __TOOL__ run "__JOB_REL__" --phase collect -y "${args[@]}"
                rc=$?
                if [ -d "__PARTS_REL__" ]; then
                  still=true
                else
                  still=false
                fi
                echo "##vso[task.setvariable variable=stillRunning;isOutput=true]$still"
                if [ "$rc" -eq 2 ]; then
                  echo "##vso[task.logissue type=warning]Some VMs are not OK (SKIPPED/ENDED/ERROR). See result.csv."
                  echo "##vso[task.complete result=SucceededWithIssues;]"
                  rc=0
                fi
                exit "$rc"
          - bash: |
              set -euo pipefail
              OUT="$(Build.ArtifactStagingDirectory)/results"
              mkdir -p "$OUT"
              if [ -f "__OUT_CSV_REL__" ]; then
                cp "__OUT_CSV_REL__" "$OUT/"
              fi
              if [ -f "__OUT_LOG_REL__" ]; then
                cp "__OUT_LOG_REL__" "$OUT/"
              fi
              if [ -d "__PARTS_REL__" ]; then
                tar -czf "$(Build.ArtifactStagingDirectory)/y2-state-collect.tgz" -C "$(Build.SourcesDirectory)" "__PARTS_REL__"
              fi
            displayName: Stage results
            condition: always()
          - publish: $(Build.ArtifactStagingDirectory)/results
            artifact: y2-results
            displayName: Publish results (csv and log)
            condition: always()
          - publish: $(Build.ArtifactStagingDirectory)/y2-state-collect.tgz
            artifact: y2-state-collect
            displayName: Publish remaining run state
            condition: and(always(), eq(variables['collect.stillRunning'], 'true'))
__RECOLLECT_STAGES__

