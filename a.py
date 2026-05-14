import os

def print_directory_tree(path, prefix=""):
    """
    Simple and fast directory tree printer
    """
    try:
        items = sorted(os.listdir(path))
        for i, item in enumerate(items):
            item_path = os.path.join(path, item)
            is_last = i == len(items) - 1
            
            if os.path.isdir(item_path):
                print(f"{prefix}{'└── ' if is_last else '├── '}📁 {item}/")
                next_prefix = prefix + ("    " if is_last else "│   ")
                print_directory_tree(item_path, next_prefix)
            else:
                # Get file size
                size = os.path.getsize(item_path)
                size_str = f"({size} bytes)" if size < 1024 else f"({size/1024:.1f} KB)"
                print(f"{prefix}{'└── ' if is_last else '├── '}📄 {item} {size_str}")
    except PermissionError:
        print(f"{prefix}└── [ACCESS DENIED]")

# Run it
print("\n" + "="*60)
print("DIRECTORY STRUCTURE")
print("="*60 + "\n")

folder = "Backtest_ENGINE"  # Change to your folder name
if not os.path.exists(folder):
    folder = "."

print(f"{os.path.basename(os.path.abspath(folder))}/")
print_directory_tree(folder)

# Save to file
output_file = "structure.txt"
with open(output_file, 'w', encoding='utf-8') as f:
    import sys
    old_stdout = sys.stdout
    sys.stdout = f
    
    print(f"{os.path.basename(os.path.abspath(folder))}/")
    print_directory_tree(folder)
    
    sys.stdout = old_stdout

print(f"\n✅ Structure saved to {output_file}")