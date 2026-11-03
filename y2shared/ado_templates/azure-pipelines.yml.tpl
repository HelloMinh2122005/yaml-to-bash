__HEADER_COMMENT__
trigger: none

parameters:
  - name: vmFilter
    displayName: Optional VM Filter (empty = all in CSV)
    type: object
    default: []
  - name: parallel
    displayName: Concurrency Budget (0 = use job.yaml)
    type: number
    default: 0

stages:
  - template: templates/y2-fleet-run.yml
    parameters:
      vmFilter: ${{ parameters.vmFilter }}
      parallel: ${{ parameters.parallel }}

