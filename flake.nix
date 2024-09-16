{
  # Essential imports for NixOS and Home Manager
  inputs = {
    nixos = {
      url = "github:NixOS/nixos";
    };
    home-manager = {
      url = "github:NixOS/home-manager";
    };
  };

  # Configuration for your home manager
  outputs = {
    self = {
      inherit inputs home-manager;
      # Configure home-manager with your desired options
      home-manager.configuration = ./home.nix;
    };
  };

  # Configuration for Hyprland
  hyprland = {
    url = "github:hyprland/hyprland";
    # Configure Hyprland with your desired options
    # For example:
    # hyprland.defaultConfig = {
    #   # ... your Hyprland configuration ...
    # };
  };

  # Configuration for the Nmap applet
  nmapplet = {
    url = "github:nmap/nmapplet";
    # Configure the Nmap applet with your desired options
    # For example:
    # nmapplet.defaultConfig = {
    #   # ... your Nmap applet configuration ...
    # };
  };

  # Declare your home manager configuration file
  home.nix = {
    # ... your home manager configuration ...
    # Include Hyprland and Nmap applet in your home configuration
    programs.hyprland = {
      enable = true;
      # ... your Hyprland configuration ...
    };
    programs.nmapplet = {
      enable = true;
      # ... your Nmap applet configuration ...
    };
  };
}
