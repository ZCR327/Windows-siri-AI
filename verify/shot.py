import _bootstrap  # noqa: F401  (repo root on sys.path)
from PIL import Image, ImageGrab
im = ImageGrab.grab()
im.crop((850, 850, 2030, 1480)).save(r"tools\siri_orb\preview_card.png")
im.crop((1080, 1130, 1860, 1770)).save(r"tools\siri_orb\preview_orb.png")
im.crop((0, 0, 760, 620)).resize((570, 465), Image.LANCZOS).save(r"tools\siri_orb\preview_corner.png")
im.resize((im.width // 2, im.height // 2), Image.LANCZOS).save(r"tools\siri_orb\preview.png")
print("captured", im.size)
