# vmware2scw v2.0 — Batch Migration Features

## Architecture

```
                         vmware2scw batch workflow
  ┌──────────────────────────────────────────────────────────────────┐
  │  1. INVENTORY + PLAN GENERATION                                  │
  │                                                                  │
  │  vmware2scw inventory-plan \                                     │
  │    --config migration.yaml \                                     │
  │    --filter "os:linux" --filter "name:prod-*" \                  │
  │    --auto-map --sizing optimize \                                │
  │    -o plan.yaml                                                  │
  │                                                                  │
  │  Output: plan.yaml (YAML reviewable/editable by operator)        │
  └──────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  2. PRE-FLIGHT ESTIMATION                                        │
  │                                                                  │
  │  vmware2scw batch estimate --plan plan.yaml                      │
  │                                                                  │
  │  ┌─────────────────────────────────────────┐                     │
  │  │ Pre-flight Migration Estimate           │                     │
  │  │ Total VMs:        12                    │                     │
  │  │ Work space:       1,860 GB              │                     │
  │  │ Est. duration:    47 min                │                     │
  │  │ Est. monthly:     €186.50/mo            │                     │
  │  │ ⚠ 3 Windows VMs require KVM + OVMF     │                     │
  │  └─────────────────────────────────────────┘                     │
  └──────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  3. BATCH EXECUTION (asyncio + semaphores)                       │
  │                                                                  │
  │  vmware2scw batch run --plan plan.yaml --config migration.yaml   │
  │                                                                  │
  │  ┌─── Wave: canary ────────────────────────────────────┐         │
  │  │  ✅ web-dev-01: ████████████████ complete (3m12s)   │         │
  │  └──────────── ⏸  pause (operator confirms) ──────────┘         │
  │                                                                  │
  │  ┌─── Wave: production ────────────────────────────────┐         │
  │  │  ☁️  web-prod-01: █████████░░░░░ uploading  (5m)    │         │
  │  │  🔧 web-prod-02: ██████░░░░░░░░ adapting   (3m)    │         │
  │  │  🔄 db-prod-01:  ████░░░░░░░░░░ converting (2m)    │         │
  │  └─────────────────────────────────────────────────────┘         │
  │                                                                  │
  │  Semaphores: esxi:4 | disk_io:3 | s3:6 | api:5 | global:10     │
  └──────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  4. REPORT + RESUME                                              │
  │                                                                  │
  │  vmware2scw batch report --batch-id abc123 -o report.md          │
  │  vmware2scw batch resume --batch-id abc123  (retry failed VMs)   │
  └──────────────────────────────────────────────────────────────────┘
```

## New / Modified Files

```
vmware2scw/
├── __init__.py
├── cli.py                          ← REPLACE (adds batch commands)
├── config.py                       ← NEW (centralized AppConfig model)
├── configs/
│   └── example_batch_plan.yaml     ← NEW (template batch plan)
├── converter/                      ← YOUR EXISTING CODE (untouched)
├── pipeline/
│   ├── __init__.py
│   ├── batch_plan.py               ← NEW (Pydantic models for batch YAML)
│   ├── batch_orchestrator.py       ← NEW (async orchestrator + report)
│   ├── dashboard.py                ← NEW (Rich live dashboard)
│   ├── inventory.py                ← NEW (filter engine + plan generator)
│   ├── migration.py                ← NEW (stage dispatcher — bridge to your code)
│   └── state.py                    ← NEW (per-VM state persistence)
├── scaleway/
│   ├── __init__.py
│   └── mapping.py                  ← NEW (instance type catalog + mapper)
├── utils/
│   ├── __init__.py
│   └── logging.py                  ← NEW (shared logger)
├── vmware/
│   ├── __init__.py
│   ├── client.py                   ← STUB (interface for your pyVmomi code)
│   └── inventory.py                ← STUB (interface for your VM collection)
└── tests/
    ├── __init__.py
    └── test_batch.py               ← NEW (33 tests, all passing)
```

## Integration Steps

### Step 1: Copy new files

All new files are standalone — they don't modify your existing converter/vmware/scaleway code.
Just copy them into your project tree.

### Step 2: Replace stubs with your existing code

Three files are **stubs** that need your actual implementation:

#### `vmware/client.py` — Replace connection stubs
```python
# In VSphereClient.connect(), replace the TODO with:
from pyVim.connect import SmartConnect, SmartConnectNoSSL
if insecure:
    self._si = SmartConnectNoSSL(host=host, user=username, pwd=password)
else:
    self._si = SmartConnect(host=host, user=username, pwd=password)
self._content = self._si.RetrieveContent()
```

#### `vmware/inventory.py` — Replace inventory stubs
```python
# In VMInventory.list_all_vms(), integrate your existing pyVmomi
# container view iteration code. Each VM should produce a VMInfo object.
```

#### `pipeline/migration.py` — Wire up stage handlers
This is the most important integration point. Each `_stage_*` method
needs to call your existing conversion code:

```python
def _stage_export(self, plan, state):
    # Call your existing VMDK export via NFC lease
    from vmware2scw.vmware.export import export_vmdk
    paths = export_vmdk(plan.vm_name, self.work_dir / state.migration_id)
    state.set_artifact("vmdk_paths", [str(p) for p in paths])

def _stage_convert(self, plan, state):
    # Call your existing qemu-img conversion
    from vmware2scw.converter.qemu import convert_vmdk_to_qcow2
    vmdk = state.get_artifact("vmdk_paths")[0]
    qcow2 = convert_vmdk_to_qcow2(vmdk)
    state.set_artifact("qcow2_path", str(qcow2))

def _stage_adapt_guest(self, plan, state):
    from vmware2scw.converter.linux_adapter import adapt_linux_guest
    adapt_linux_guest(state.get_artifact("qcow2_path"))

# etc.
```

### Step 3: Update pyproject.toml

No new dependencies! Everything uses what you already have:
- `pydantic` ≥ 2.0
- `pyyaml`
- `click`
- `rich`
- `asyncio` (stdlib)

Just add the new entry point:
```toml
[project.scripts]
vmware2scw = "vmware2scw.cli:main"
```

### Step 4: Validate

```bash
# Run tests (33 should pass)
python -m pytest vmware2scw/tests/test_batch.py -v

# Test CLI help
python -m vmware2scw.cli --help
python -m vmware2scw.cli batch --help
```

## Feature Reference

### inventory-plan — Filter + Auto-map + Export

```bash
# All Linux VMs, auto-map, export plan
vmware2scw inventory-plan --config migration.yaml \
  --filter "os:linux" --auto-map -o plan.yaml

# Production web servers with >4 CPUs
vmware2scw inventory-plan -f "name:web-prod-*" -f "os:linux" \
  --min-cpu 4 --sizing optimize -o plan.yaml

# Windows VMs in specific folder
vmware2scw inventory-plan -f "folder:/DC1/Production/Windows" \
  -o windows-plan.yaml

# All powered-on VMs on specific ESXi hosts
vmware2scw inventory-plan -f "state:poweredOn" -f "host:esxi-0[12]*" \
  --auto-map --tag migrated-from-vmware -o plan.yaml
```

**Supported filters:**
| Filter | Example | Description |
|--------|---------|-------------|
| `name:` | `name:web-*` | Glob pattern on VM name |
| `regex:` | `regex:^prod-\d+` | Regex on VM name |
| `folder:` | `folder:/DC1/Production` | vCenter folder prefix |
| `os:` | `os:linux`, `os:windows` | OS family |
| `host:` | `host:esxi-01*` | ESXi host glob |
| `cluster:` | `cluster:prod-*` | vCenter cluster |
| `dc:` | `dc:DC1` | Datacenter |
| `state:` | `state:poweredOn` | Power state |
| `firmware:` | `firmware:bios` | Firmware type |
| `--min-cpu` | `--min-cpu 4` | Minimum vCPU count |
| `--max-disk` | `--max-disk 500` | Maximum total disk (GB) |

### batch estimate — Pre-flight Check

```bash
vmware2scw batch estimate --plan plan.yaml --available-disk 10000
```

Reports: disk space needed, estimated duration, monthly cost, warnings.

### batch run — Execute with Dashboard

```bash
# Interactive (with confirmation)
vmware2scw batch run --plan plan.yaml --config migration.yaml

# Non-interactive
vmware2scw batch run --plan plan.yaml --config migration.yaml -y

# With report output
vmware2scw batch run --plan plan.yaml --config migration.yaml \
  --report report.md
```

### batch resume — Retry Failed VMs

```bash
vmware2scw batch resume --batch-id abc12345 --config migration.yaml
```

Retries all failed VMs from their last successful stage. Completed VMs
are skipped.

### batch status / report

```bash
vmware2scw batch status                        # Most recent batch
vmware2scw batch status --batch-id abc12345    # Specific batch
vmware2scw batch report --batch-id abc12345 -o report.md
```

## YAML Batch Plan Format

See `configs/example_batch_plan.yaml` for the complete reference.

Key sections:
- **defaults**: Zone, sizing strategy, tags (applied to all VMs)
- **concurrency**: Per-resource semaphore limits
- **migrations**: VM list with selectors, overrides, priorities, waves
- **exclude**: VMs to skip (patterns, regex)
- **waves**: Staged rollout groups with pause policies
- **post_migration**: Source VM tagging, cleanup

## Concurrency Model

The orchestrator uses **per-resource semaphores** (not a single global limit):

| Semaphore | Default | Bottleneck |
|-----------|---------|------------|
| `per_esxi_host` | 4 | NFC lease limit per ESXi host |
| `disk_io` | 3 | SBS volume IOPS (conversion) |
| `s3_upload` | 6 | S3 bandwidth (~10 Gbps shared) |
| `scw_api` | 5 | Scaleway API rate (~50 req/min) |
| `global` | 10 | Total concurrent VM pipelines |

This means: a VM uploading to S3 doesn't block another VM from converting.
Each stage acquires only the semaphore it needs.

## State & Resume

- Batch state saved to `{work_dir}/batch-state/batch-{id}.json`
- Updated after every VM completion (crash-safe)
- `batch resume` resets failed VMs to pending and reruns them
- Completed VMs are never re-executed
- Stage-level granularity: if a VM failed at "upload_s3", resume
  skips validate/snapshot/export/convert and retries from upload

## Wave Execution

Waves provide staged rollout for production environments:

```yaml
waves:
  - name: canary
    vms: ["web-dev-01"]
    pause_after: pause              # Always wait for operator

  - name: dev
    vms: ["dev-*"]
    pause_after: pause_on_failure   # Auto-continue if all OK

  - name: production
    vms: ["web-prod-*", "db-*"]
    pause_after: pause              # Always wait before Windows

  - name: windows
    vms: ["win-*"]
    pause_after: continue           # Last wave, no pause
```

Options: `pause`, `continue`, `pause_on_failure`
