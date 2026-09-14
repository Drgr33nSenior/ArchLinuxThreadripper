SHELL := /bin/bash

.PHONY: check check-strict syntax test bats shellcheck shfmt yaml ansible kubernetes systemd

check: syntax shellcheck shfmt yaml test bats ansible kubernetes systemd

check-strict:
	@bash tests/check-strict.sh

syntax:
	@bash tests/syntax.sh

test:
	@bash tests/run.sh

shellcheck:
	@set -euo pipefail; if command -v shellcheck >/dev/null 2>&1; then \
		files=$$(rg --files -g '*.sh' -g 'bin/*' -g 'PKGBUILD' -g 'launch-*'); \
		[[ -n $$files ]] || { echo 'FAIL: shellcheck discovery returned no files' >&2; exit 1; }; \
		files=$$(printf '%s\n' "$$files" templates/arch/uki-sync templates/workstation/restic/workstation-restic | sort -u); \
		args=(); while IFS= read -r file; do args+=("$$file"); done <<<"$$files"; \
		shellcheck -x "$${args[@]}"; \
	else \
		echo 'SKIP: shellcheck is not installed'; \
	fi

shfmt:
	@set -euo pipefail; if command -v shfmt >/dev/null 2>&1; then \
		files=$$(rg --files -g '*.sh' -g 'bin/*'); \
		[[ -n $$files ]] || { echo 'FAIL: shfmt discovery returned no files' >&2; exit 1; }; \
		files=$$(printf '%s\n' "$$files" templates/arch/uki-sync templates/workstation/restic/workstation-restic | sort -u); \
		args=(); while IFS= read -r file; do args+=("$$file"); done <<<"$$files"; \
		shfmt -d -i 2 -ci "$${args[@]}"; \
	else \
		echo 'SKIP: shfmt is not installed'; \
	fi

yaml:
	@set -euo pipefail; if command -v ruby >/dev/null 2>&1; then \
		files=$$(find ansible cloud-init kubernetes -type f \( -name '*.yml' -o -name '*.yaml' -o -name '*.tmpl' \) ! -name '*.j2' | sort); \
		[[ -n $$files ]] || { echo 'FAIL: YAML discovery returned no files' >&2; exit 1; }; \
		extra=$$(rg --files infrastructure apps -g '*.yml' -g '*.yaml'); \
		[[ -n $$extra ]] || { echo 'FAIL: workload YAML discovery returned no files' >&2; exit 1; }; \
		args=(); while IFS= read -r file; do args+=("$$file"); done <<<"$$files"; \
		while IFS= read -r file; do args+=("$$file"); done <<<"$$extra"; \
		ruby -ryaml -e 'ARGV.each { |path| YAML.load_stream(File.read(path)) }' "$${args[@]}"; \
	else \
		echo 'SKIP: Ruby YAML parser is unavailable'; \
	fi

ansible:
	@if command -v ansible-playbook >/dev/null 2>&1 && ansible-playbook --version >/dev/null 2>&1; then \
		ANSIBLE_ROLES_PATH=ansible/roles ansible-playbook --syntax-check ansible/k3s.yml && \
		ANSIBLE_ROLES_PATH=ansible/roles ansible-playbook --syntax-check ansible/k3s-restore-test.yml && \
		ansible-playbook -i infrastructure/ansible/inventory.example.ini --syntax-check infrastructure/ansible/site.yml; \
	else \
		echo 'SKIP: a working ansible-playbook is unavailable'; \
	fi

kubernetes:
	@if command -v kubectl >/dev/null 2>&1; then \
		kubectl kustomize kubernetes >/dev/null; \
	else \
		echo 'SKIP: kubectl is unavailable'; \
	fi

bats:
	@if command -v bats >/dev/null 2>&1; then \
		bats tests/workstation/runtime.bats; \
	else \
		echo 'SKIP: bats is unavailable'; \
	fi

systemd:
	@if [ "$$(uname -s)" = Linux ] && command -v systemd-analyze >/dev/null 2>&1; then \
		systemd-analyze verify templates/workstation/restic/workstation-restic@.service \
			templates/workstation/restic/workstation-restic-backup.timer \
			templates/workstation/restic/workstation-restic-retention.timer \
			templates/workstation/telemetry/workstation-telemetry.service \
			templates/workstation/telemetry/workstation-telemetry.timer \
			templates/workstation/telemetry/workstation-alloy.service \
			ansible/roles/k3s/files/k3s-lab-firewall.service; \
	else \
		echo 'SKIP: systemd-analyze verification requires Linux'; \
	fi
