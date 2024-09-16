{
  description = "NixOS configuration with Hyprland and LightDM";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    flake-utils.url = "github:numtide/flake-utils";
    home-manager.url = "github:nix-community/home-manager";
  };

  outputs = { nixpkgs, flake-utils, home-manager, ... }:
    flake-utils.lib.eachDefaultSystem (system:
    let
      pkgs = import nixpkgs { inherit system; };
    in {
      nixosConfigurations.my-nixos-config = pkgs.lib.nixosSystem {
        system = "x86_64-linux";  # oder aarch64-linux je nach Architektur

        modules = [
          ./configuration.nix
          home-manager.nixosModules.home-manager

          # Konfiguration für Hyprland und LightDM
          {
            environment.systemPackages = with pkgs; [
              hyprland
              hyprpaper
              waybar
              wofi
              xwayland
              kitty   # Optional
              networkmanagerapplet
            ];

            # LightDM als Display Manager aktivieren
            services.xserver = {
              enable = true;
              layout = "de";  # Tastaturlayout
              displayManager.lightdm = {
                enable = true;
                greeters = [ pkgs.lightdm-mini-greeter ];  # Minimaler Greeter für LightDM
              };
              desktopManager.default = "none";  # Deaktiviert den Standard-Desktop-Manager
              windowManager.hyprland.enable = true;  # Hyprland als WM aktivieren
            };

            services.pipewire = {
              enable = true;
              alsa.enable = true;
              pulse.enable = true;
            };

            # Optionale Konfiguration für LightDM, um Hyprland korrekt zu starten
            programs.lightdm.settings = {
              user-session = "hyprland";
              minimum-vt = 7;
            };
          }
        ];
      };
    });
}
