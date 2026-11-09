import UI from './app/ui.js';

// Switch through the current VNC connection and Openbox shortcuts.
// No extra listener, profile port, or HTTP control endpoint is required.
const toolbar = document.createElement('nav');
toolbar.id = 'acf-profiles';
toolbar.setAttribute('aria-label', 'Browser profiles');
const label = document.createElement('span');
label.textContent = 'Browser';
toolbar.append(label);

const buttons = ['Primary', 'Secondary'].map((name, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = name;
    button.title = `Show ${name.toLowerCase()} profile (Ctrl+Alt+${index + 1})`;
    button.disabled = true;
    button.addEventListener('click', () => {
        // noVNC replaces the RFB instance on reconnect. Never retain a stale one.
        const rfb = UI.rfb;
        if (!UI.connected || !rfb || rfb.viewOnly) return;
        rfb.sendKey(0xffe3, 'ControlLeft', true);
        rfb.sendKey(0xffe9, 'AltLeft', true);
        rfb.sendKey(0x31 + index, `Digit${index + 1}`, true);
        rfb.sendKey(0x31 + index, `Digit${index + 1}`, false);
        rfb.sendKey(0xffe9, 'AltLeft', false);
        rfb.sendKey(0xffe3, 'ControlLeft', false);
        rfb.focus();
    });
    toolbar.append(button);
    return button;
});

document.body.prepend(toolbar);
function updateAvailability() {
    const disabled = !UI.connected || !UI.rfb || UI.rfb.viewOnly;
    for (const button of buttons) button.disabled = Boolean(disabled);
}
updateAvailability();
setInterval(updateAvailability, 250);
