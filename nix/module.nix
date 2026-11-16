{ config, lib, pkgs, ... }:
let
  cfg = config.services.canvas-api;
  stateDir = "/var/lib/canvas-api";
  runtimeCreds = "/run/canvas-api/credentials.env";
  passkeyFile = "${stateDir}/passkeys.json";
  backupDir = "/var/backups/canvas-api";
  passkeyBackup = "${backupDir}/passkeys.json";

  # Root-owned, add-only union of every passkey the service has saved. A key's private
  # half cannot be recovered once lost, so nothing here ever removes a credential:
  # per credentialId the highest signCount wins and a known rpId is never forgotten.
  backupPasskeys = pkgs.writeShellApplication {
    name = "canvas-api-backup-passkeys";
    runtimeInputs = [ pkgs.coreutils pkgs.jq ];
    text = ''
      install -d -o root -g root -m 0700 ${backupDir}
      sources=()
      for f in ${passkeyBackup} ${passkeyFile}; do
        if [[ -s "$f" ]] && jq -e '.credentials | type == "array"' "$f" >/dev/null 2>&1; then
          sources+=("$f")
        else
          [[ ! -e "$f" ]] || echo "ignoring unreadable passkey file $f" >&2
        fi
      done
      (( ''${#sources[@]} )) || exit 0
      tmp=$(mktemp ${backupDir}/.passkeys.XXXXXX)
      trap 'rm -f -- "$tmp"' EXIT
      jq -s '{credentials: ([.[].credentials[] | select(.credentialId and .privateKey)]
              | group_by(.credentialId)
              | map(max_by(.signCount // 0) + {rpId: ([.[].rpId | select(. != null)] | first)}))}' \
        "''${sources[@]}" > "$tmp"
      if [[ "$(jq '.credentials | length' "$tmp")" -gt 0 ]] && ! cmp -s "$tmp" ${passkeyBackup}; then
        chmod 0600 "$tmp"
        mv "$tmp" ${passkeyBackup}
        echo "passkey backup updated: $(jq '.credentials | length' ${passkeyBackup}) key(s)"
      fi
    '';
  };

  # Root half of startup: create an API key once, hand credentials to the service user.
  prepare = pkgs.writeShellApplication {
    name = "canvas-api-prepare";
    runtimeInputs = [ pkgs.coreutils pkgs.openssl ];
    text = ''
      if [[ ! -s ${stateDir}/api-key ]]; then
        (umask 027; openssl rand -hex 32 > ${stateDir}/api-key.tmp)
        chown canvas-api:canvas-api ${stateDir}/api-key.tmp
        mv ${stateDir}/api-key.tmp ${stateDir}/api-key
      fi
      chmod 0640 ${stateDir}/api-key
      rm -f ${runtimeCreds}
      # Back up, then restore any key missing from (or older in) the service's own file.
      ${backupPasskeys}/bin/canvas-api-backup-passkeys
      if [[ -s ${passkeyBackup} ]] && ! cmp -s ${passkeyBackup} ${passkeyFile}; then
        install -o canvas-api -g canvas-api -m 0600 ${passkeyBackup} ${passkeyFile}
        echo "restored passkeys from ${passkeyBackup}"
      fi
      if [[ -f ${lib.escapeShellArg cfg.credentialsFile} ]]; then
        install -o canvas-api -g canvas-api -m 0400 ${lib.escapeShellArg cfg.credentialsFile} ${runtimeCreds}
      fi
    '';
  };

  setCredentials = pkgs.writeShellApplication {
    name = "canvas-api-credentials";
    runtimeInputs = [ pkgs.coreutils pkgs.systemd ];
    text = ''
      [[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
      file=${lib.escapeShellArg cfg.credentialsFile}
      read -rp "NetID or email: " username
      read -rsp "Password: " password; echo
      read -rsp "Authenticator TOTP secret (optional, Enter to skip): " totp; echo
      install -d -m 0700 "$(dirname "$file")"
      tmp=$(mktemp "$file.XXXXXX")
      chmod 0600 "$tmp"
      {
        printf 'CANVAS_USERNAME=%s\n' "$username"
        printf 'CANVAS_PASSWORD=%s\n' "$password"
        [[ -z "$totp" ]] || printf 'CANVAS_TOTP_SECRET=%s\n' "$totp"
      } > "$tmp"
      mv "$tmp" "$file"
      systemctl restart canvas-api.service
      echo "Saved to $file and restarted canvas-api."
    '';
  };

  showKey = pkgs.writeShellApplication {
    name = "canvas-api-key";
    text = ''cat ${stateDir}/api-key'';
  };
in
{
  options.services.canvas-api = {
    enable = lib.mkEnableOption "the headless-browser Canvas API service";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
    };
    baseUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://canvas.tamu.edu";
      description = "Canvas instance to log in to.";
    };
    emailDomain = lib.mkOption {
      type = lib.types.str;
      default = "tamu.edu";
      description = "Appended to a bare NetID to form the Microsoft sign-in name.";
    };
    listenAddress = lib.mkOption { type = lib.types.str; default = "0.0.0.0"; };
    port = lib.mkOption { type = lib.types.port; default = 8787; };
    openFirewallOnInterfaces = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "tailscale0" ];
      description = "Interfaces on which the port is opened. Nothing is opened publicly.";
    };
    credentialsFile = lib.mkOption {
      type = lib.types.str;
      default = "/etc/canvas-api/credentials.env";
      description = ''
        Root-only KEY=VALUE file with CANVAS_USERNAME, CANVAS_PASSWORD and optionally
        CANVAS_TOTP_SECRET. Kept out of the Nix store; write it with `sudo canvas-api-credentials`.
      '';
    };
    keyReaders = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Users added to the canvas-api group, allowing them to read the API key.";
    };
    keepaliveSeconds = lib.mkOption { type = lib.types.ints.positive; default = 600; };
  };

  config = lib.mkIf cfg.enable {
    users.groups.canvas-api = { };
    users.users = {
      canvas-api = {
        isSystemUser = true;
        group = "canvas-api";
        home = stateDir;
      };
    } // lib.genAttrs cfg.keyReaders (_: { extraGroups = [ "canvas-api" ]; });

    networking.firewall.interfaces = lib.genAttrs cfg.openFirewallOnInterfaces
      (_: { allowedTCPPorts = [ cfg.port ]; });

    environment.systemPackages = [ setCredentials showKey ];

    # Back up the passkey file whenever the service rewrites it (and hourly as a fallback).
    systemd.paths.canvas-api-passkey-backup = {
      wantedBy = [ "multi-user.target" ];
      pathConfig.PathChanged = passkeyFile;
    };
    systemd.services.canvas-api-passkey-backup = {
      description = "Back up canvas-api passkeys (add-only)";
      startAt = "hourly";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${backupPasskeys}/bin/canvas-api-backup-passkeys";
        UMask = "0077";
      };
    };

    systemd.services.canvas-api = {
      description = "Canvas API (headless browser session for ${cfg.baseUrl})";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = {
        CANVAS_BASE_URL = cfg.baseUrl;
        CANVAS_API_EMAIL_DOMAIN = cfg.emailDomain;
        CANVAS_API_HOST = cfg.listenAddress;
        CANVAS_API_PORT = toString cfg.port;
        CANVAS_API_STATE_DIR = stateDir;
        CANVAS_API_KEY_FILE = "${stateDir}/api-key";
        CANVAS_API_CREDENTIALS_FILE = runtimeCreds;
        CANVAS_API_KEEPALIVE_SECONDS = toString cfg.keepaliveSeconds;
        HOME = stateDir;
        XDG_CACHE_HOME = "${stateDir}/.cache";
      };
      serviceConfig = {
        User = "canvas-api";
        Group = "canvas-api";
        StateDirectory = "canvas-api";
        StateDirectoryMode = "0750";
        RuntimeDirectory = "canvas-api";
        RuntimeDirectoryMode = "0700";
        WorkingDirectory = stateDir;
        ExecStartPre = [ "+${prepare}/bin/canvas-api-prepare" ];
        ExecStart = lib.getExe cfg.package;
        Restart = "on-failure";
        RestartSec = "10s";
        UMask = "0077";
        MemoryHigh = "1500M";
        MemoryMax = "2G";
        CPUQuota = "150%";
        TasksMax = 512;
        LimitCORE = 0;
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
        LockPersonality = true;
        RemoveIPC = true;
      };
    };
  };
}
