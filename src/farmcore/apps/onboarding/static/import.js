// Native file chooser remains usable without JavaScript.
const zone = document.querySelector('[data-parcel-drop]');
if (zone) {
  const input = zone.querySelector('input[type="file"]');
  const status = document.querySelector('[data-import-status]');
  const describe = () => { status.textContent = input.files[0]?.name || 'No file selected.'; };
  input.addEventListener('change', describe);
  zone.addEventListener('dragover', (event) => event.preventDefault());
  zone.addEventListener('drop', (event) => {
    event.preventDefault();
    if (event.dataTransfer.files.length !== 1) {
      status.textContent = 'Choose one parcel file at a time.';
      return;
    }
    input.files = event.dataTransfer.files;
    describe();
  });
  zone.closest('form').addEventListener('submit', (event) => {
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    status.textContent = 'Reading parcels…';
  });
}
