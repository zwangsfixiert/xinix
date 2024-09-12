# ~/config/home/flake.nix
{
  description = "Home Manager Configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    home-manager.url = "github:nix-community/home-manager";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, home-manager }: {
    homeConfigurations = {
      yourusername = home-manager.lib.homeManagerConfiguration {
        pkgs = nixpkgs.lib;
        system = "x86_64-linux";
        homeDirectory = "/home/strizzi";  # Replace with your username
        stateVersion = "24.05";  # Set the current version of NixOS

        configuration = {
          # Home Manager programs and packages
          programs.home-manager.enable = true;

          # Hyprland Configuration
          programs.hyprland = {
            enable = true;
            config = {
              general = {
                layout = "us";   # Set keyboard layout
                sensitivity = "0.5";  # Mouse sensitivity
              };
            };
          };

          # Add user-specific packages
          home.packages = with pkgs; [
            hyprland  # Hyprland compositor
            waybar    # Status bar
            alacritty # Terminal emulator
            firefox   # Web browser
            neovim    # Code editor
          ];
        };
      };
    };
  };
}
