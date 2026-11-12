# Contributions and repository access

Fork and modify the project under its CC BY-SA 4.0 licence. Keep upstream
attribution and identify modifications. Pull requests may be proposed; only
maintainers can approve and merge changes into this repository. Forking does
not grant write access here. Do not include credentials or personal deployment
details in code, screenshots, issues or pull requests.

Run the hardware-free tests before proposing changes:

```sh
python3 -m unittest discover -s tests -v
```

Tests must not issue real shutdown commands or change actual GPIO settings.
Describe hardware/OS versions tested and distinguish simulation from hardware
validation. Charging and shutdown enablement must retain separate double
confirmations. Merely viewing the dashboard must never change settings; settings
actions must use the separately privileged helper and both confirmations. Preserve
view-only and demo modes as non-mutating.
