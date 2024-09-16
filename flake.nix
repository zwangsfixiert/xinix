{
  # Hyprland configuration
  hyprland.version = "24.05";
  hyprland.enable = true;
  hyprland.config = "/etc/hypr/hypr.conf";

  # Xorg configuration
  xorg.enable = true;
  xorg.server = "Xorg";

  # Input devices
  input.devices = {
    # Configure keyboard and mouse devices here
  };

  # Compositor modules
  modules = [
    # Essential modules
    "picom" # For compositing effects
    "xcursor" # For mouse cursor management
    "xkeyboard-config" # For keyboard layout and configuration
    "xrandr" # For screen resolution and orientation management

    # Optional modules (add as needed)
    "autorandr" # For automatic screen configuration
    "brightness" # For screen brightness control
    "keyboard-layout" # For keyboard layout switching
    "swaylock" # For screen locking
    "lightdm" # For display manager
    "networkmanagerapplet" # For network manager applet
  ];

  # Services
  services = [
    # Start Hyprland, Xorg, lightdm, and networkmanagerapplet services
    "hyprland"
    "xorg"
    "lightdm"
    "networkmanagerapplet"
  ];
}
