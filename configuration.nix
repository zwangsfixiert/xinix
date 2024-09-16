{ config, pkgs, ... }:

{
  imports = [
    ./hardware-configuration.nix
  ];

  # NixOS Basis-Services
  boot.loader.systemd-boot.enable = true;
  networking.hostName = "nixos";

  # Weitere Standardkonfigurationen
}
