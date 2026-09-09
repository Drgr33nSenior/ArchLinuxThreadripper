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
	@if command -v shellcheck >/dev/null 2>&1; then \
		{ rg --files -g '*.sh' -g 'bin/*' -g 'PKGBUILD' -g 'launch-*'; printf '%s\n' templates/arch/uki-sync templates/workstation/restic/workstation-restic; } | sort -u | xargs shellcheck -x; \
	else \
		echo 'SKIP: shellcheck is not installed'; \
	fi

shfmt:
	@if command -v shfmt >/dev/null 2>&1; then \
		{ rg --files -g '*.sh' -g 'bin/*'; printf '%s\n' templates/arch/uki-sync templates/workstation/restic/workstation-restic; } | sort -u | xargs shfmt -d -i 2 -ci; \
	else \
		echo 'SKIP: shfmt is not installed'; \
	fi

yaml:
	@if command -v ruby >/dev/null 2>&1; then \
		ruby -ryaml -e 'ARGV.each { |path| YAML.load_stream(File.read(path)) }' \
			$$(find ansible cloud-init kubernetes -type f \( -name '*.yml' -o -name '*.yaml' -o -name '*.tmpl' \) ! -name '*.j2' | sort) && \
		ruby -ryaml -e 'ARGV.each { |path| YAML.load_stream(File.read(path)) }' $$(rg --files infrastructure apps -g '*.yml' -g '*.yaml'); \
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
			ansible/roles/k3s/files/k3s-lab-firewall.service; \
	else \
		echo 'SKIP: systemd-analyze verification requires Linux'; \
	fi
