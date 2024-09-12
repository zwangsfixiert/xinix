# /etc/nixos/configuration.nix
{ config, pkgs, ... }:

{
  # Enable Flakes for package management
  nix = {
    package = pkgs.nixFlakes;
    extraOptions = ''
      experimental-features = nix-command flakes;
    '';
  };

  # Enable Wayland and disable X11
  services.xserver.enable = false;

  # Enable Hyprland (Wayland window manager)
  services.xserver.windowManager.hyprland.enable = true;

  # Enable NetworkManager for managing network connections
  networking.networkmanager.enable = true;

  # Enable PipeWire for sound
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    pulse.enable = true;
    jack.enable = true;
  };

  # Enable Home Manager using Flakes
  home-manager = {
    useGlobalPkgs = true;
    useUserPackages = true;
    users = {
      strizzi = {  # Replace with your actual username
        home.stateVersion = "24.05";  # Replace with the current NixOS version
        programs.home-manager.enable = true;
      };
    };
  };

  # Add system-wide packages
  environment.systemPackages = with pkgs; [
    vim                # A text editor
    htop               # System monitor
    hyprland           # The Hyprland compositor
    alacritty          # Terminal emulator
    waybar             # Status bar for Hyprland
    wl-clipboard       # Clipboard utilities for Wayland
    swaylock           # Lock screen for Wayland
    firefox            # Web browser
  ];

  # Enable seatd for Hyprland session management
  systemd.services.seatd = {
    enable = true;
    wantedBy = [ "graphical.target" ];
  };

  # Set user permissions
  users.users.strizzi = {  # Replace 'yourusername' with your actual username
    isNormalUser = true;
    extraGroups = [ "wheel" "networkmanager" ];  # Add necessary groups
    packages = with pkgs; [
      hyprland    # Add Hyprland for the user
    ];
  };

  # Enable auto-login for LightDM (optional)
  services.xserver.displayManager.lightdm.autoLogin.enable = true;
  services.xserver.displayManager.lightdm.autoLogin.user = "strizzi"; # Replace with your user

  # Allow unfree packages (e.g., for proprietary drivers)
  nixpkgs.config.allowUnfree = true;

  # Enable time services (NTP)
  time.timeZone = "Europe/Vienna";  # Set your timezone
  services.ntp.enable = true;
}
