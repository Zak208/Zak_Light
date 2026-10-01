import tray


def test_menu_is_built_from_state_and_actions_work(state):
    menu = tray.build_menu(state, lambda *a: None, lambda *a: None, "9.9")
    labels = [i.text for i in menu.items if getattr(i, "text", None)]
    assert "Salir" in labels and "Perfiles" in labels and "Pantalla" in labels

    state.switch_mode("rhythm")
    mode_items = {i.text: i for i in menu.items if i.text in dict(label[::-1] for label in tray.MODE_LABELS)}
    assert mode_items["Ritmo y audio"].checked is True
    assert mode_items["Pantalla"].checked is False


def test_icon_follows_strip_color_and_greys_out_when_off(state):
    state.switch_mode("static")
    state.static_color_rgb = [255, 0, 0]
    rgb, off = tray.strip_color(state)
    assert not off and rgb[0] > 200 and rgb[1] < 40
    assert tray.icon_image(rgb).size == (64, 64)

    state.switch_mode("off")
    rgb, off = tray.strip_color(state)
    assert rgb is None and off is True


def test_profile_items_list_saved_profiles(state):
    state.profiles.save("Cine", state.config)
    menu = tray.build_menu(state, lambda *a: None, lambda *a: None, "1")
    sub = next(i for i in menu.items if i.text == "Perfiles").submenu
    assert [i.text for i in sub.items] == ["Cine"]


def test_tray_icon_is_the_logo_tinted_with_the_strip_color():
    red = tray.icon_image((255, 0, 0))
    grey = tray.icon_image(None, off=True)
    assert red.size == (64, 64) and red.getpixel((1, 1))[3] == 0              # rounded corners stay transparent
    import numpy as np
    px, off = np.asarray(red).reshape(-1, 4), np.asarray(grey).reshape(-1, 4)
    lit = (px[:, 3] > 0) & (px[:, 0] > 200) & (px[:, 1] < 60)                # red lettering on the dark plate
    assert lit.sum() > 300
    assert not ((off[:, 3] > 0) & (off[:, 0] > 200) & (off[:, 1] < 60)).any()   # off: no colour at all


def test_logo_files_ship_with_the_window():
    import os

    from core.paths import resource_dir
    static = os.path.join(resource_dir(), "web", "static")
    for name in ("favicon.ico", "logo.png", "logo-word.png"):
        assert os.path.getsize(os.path.join(static, name)) > 500, name
