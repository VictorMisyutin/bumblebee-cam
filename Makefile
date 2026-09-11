# Pollinator field camera. Run `make help` for targets.
SHELL := /bin/bash
PREFIX      := /opt/pollinator
CONFDIR     := /etc/pollinator
UNITDIR     := /etc/systemd/system
BINDIR      := /usr/local/bin
SERVICES    := pollinator-cam.service pollinator-field.service

.PHONY: help deploy code config units ops restart status logs live diff clean freeze

help:
	@echo "make deploy   - push code + units + ops to the system, restart services"
	@echo "make code     - copy src/*.py to $(PREFIX) and restart cam"
	@echo "make config   - copy baseline.json to $(CONFDIR) (never touches device.json)"
	@echo "make units    - copy systemd units and drop-ins, daemon-reload"
	@echo "make ops      - copy ops/*.sh to $(BINDIR)"
	@echo "make restart  - restart both services"
	@echo "make status   - is-active + recent journal lines"
	@echo "make logs     - follow the capture service log"
	@echo "make live     - show latest heartbeat and live-frame age"
	@echo "make diff     - show drift between repo and deployed files"
	@echo "make freeze   - copy deployed files BACK into the repo"

code:
	sudo install -m 0644 src/*.py $(PREFIX)/
	sudo systemctl restart pollinator-cam.service

config:
	sudo install -m 0644 config/baseline.json $(CONFDIR)/

units:
	sudo install -m 0644 systemd/*.service $(UNITDIR)/
	@for d in systemd/*.service.d; do \
	  [ -d "$$d" ] || continue; \
	  sudo install -d $(UNITDIR)/$$(basename $$d); \
	  sudo install -m 0644 $$d/*.conf $(UNITDIR)/$$(basename $$d)/; \
	done
	sudo systemctl daemon-reload

ops:
	sudo install -m 0755 ops/*.sh $(BINDIR)/

deploy: code config units ops restart

restart:
	sudo systemctl restart $(SERVICES)

status:
	@systemctl is-active $(SERVICES) || true
	@echo
	@systemctl --no-pager --lines=5 status pollinator-cam.service || true

logs:
	journalctl -u pollinator-cam.service -f

live:
	@tail -1 /var/log/heartbeat.log
	@echo -n "live frame age: "; \
	  echo $$(( $$(date +%s) - $$(stat -c %Y /run/pollinator/live.jpg) ))s

diff:
	@for f in src/*.py; do \
	  diff -q $$f $(PREFIX)/$$(basename $$f) >/dev/null 2>&1 || echo "DRIFT: $$f"; \
	done
	@diff -q config/baseline.json $(CONFDIR)/baseline.json >/dev/null 2>&1 \
	  || echo "DRIFT: config/baseline.json"
	@for f in systemd/*.service; do \
	  diff -q $$f $(UNITDIR)/$$(basename $$f) >/dev/null 2>&1 || echo "DRIFT: $$f"; \
	done
	@echo "(no output above means repo matches deployed)"

freeze:
	cp $(PREFIX)/pollinator_common.py $(PREFIX)/pollinator_cam.py \
	   $(PREFIX)/pollinator_field.py $(PREFIX)/pollinator_live.py src/
	sudo cp $(CONFDIR)/baseline.json config/
	sudo cp $(UNITDIR)/pollinator-cam.service $(UNITDIR)/pollinator-field.service systemd/
	sudo chown -R $$(id -un):$$(id -gn) src config systemd
	@git status --short

clean:
	rm -rf src/__pycache__
