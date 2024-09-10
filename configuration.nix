{
  # Nix mit Flakes-Unterstützung konfigurieren
  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  # Home Manager als Modul in NixOS integrieren
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    home-manager.url = "github:nix-community/home-manager/release-24.05";
  };

  outputs = { nixpkgs, home-manager, ... }: {
    nixosConfigurations = {
      # Dein Hostname, z.B. "my-nixos"
      my-nixos = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";  # oder "aarch64-linux" je nach Architektur
        modules = [
          ./configuration.nix
          home-manager.nixosModules.home-manager
        ];
      };
    };
  };

  # Systemkonfiguration
  { config, pkgs, ... }:
  {
    # Git installieren
    environment.systemPackages = with pkgs; [
      git
      hyprland
      alacritty
      waybar
      rofi
      mako
    ];

    # Home Manager für den Benutzer einrichten
    home-manager.useUserPackages = true;
    home-manager.users.your_username = {  # Ersetze 'your_username' mit deinem Benutzernamen
      home.stateVersion = "24.05";  # Entspricht der aktuellen Version von Home Manager
      programs.home-manager.enable = true;
    };

    # Hyprland und Abhängigkeiten installieren
    services.xserver = {
      enable = true;
      layout = "de";  # Tastatur-Layout
      xkbOptions = "eurosign:e";
      displayManager.sddm.enable = true;
      desktopManager.hyprland.enable = true;
    };

    # Optional: Nützliche Tools
    programs.swaylock.enable = true;
  };
}
