document.addEventListener('DOMContentLoaded', function () {
    initNavbar();
    initGpsLocation();
    initImagePreview();
    initAutoCategorize();
    initStatCounters();
    initNotifDropdown();
    initHistoryFilters();
    initVoiceInput();
    initLanguageSwitcher();
    initImageDuplicateCheck();
});

function initNavbar() {
    const toggle = document.querySelector('.nav-toggle');
    const menu = document.querySelector('.nav-menu');
    if (!toggle || !menu) return;

    toggle.addEventListener('click', function () {
        menu.classList.toggle('open');
        toggle.setAttribute('aria-expanded', menu.classList.contains('open'));
    });

    document.addEventListener('click', function (e) {
        if (!e.target.closest('.site-header')) {
            menu.classList.remove('open');
        }
    });

    const path = window.location.pathname;
    document.querySelectorAll('.nav-menu a').forEach(function (link) {
        if (link.getAttribute('href') === path) {
            link.classList.add('active');
        }
    });
}

function initGpsLocation() {
    const btn = document.getElementById('btn-detect-location');
    if (!btn) return;

    const latInput = document.getElementById('latitude');
    const lngInput = document.getElementById('longitude');
    const addressInput = document.getElementById('gps_address');
    const statusEl = document.getElementById('gps-status');
    const coordsEl = document.getElementById('gps-coords');

    btn.addEventListener('click', function () {
        if (!navigator.geolocation) {
            setGpsStatus(statusEl, 'Geolocation is not supported by your browser.', 'error');
            return;
        }

        btn.disabled = true;
        btn.textContent = 'Detecting...';
        setGpsStatus(statusEl, 'Acquiring GPS coordinates...', '');

        navigator.geolocation.getCurrentPosition(
            function (pos) {
                const lat = pos.coords.latitude;
                const lng = pos.coords.longitude;
                const acc = pos.coords.accuracy;

                if (latInput) latInput.value = lat.toFixed(7);
                if (lngInput) lngInput.value = lng.toFixed(7);

                if (coordsEl) {
                    coordsEl.textContent = 'Lat: ' + lat.toFixed(6) + ' | Lng: ' + lng.toFixed(6) + ' | Accuracy: ~' + Math.round(acc) + 'm';
                }

                setGpsStatus(statusEl, 'Location captured successfully.', 'success');
                btn.textContent = 'Refresh Location';
                btn.disabled = false;

                reverseGeocode(lat, lng, addressInput);
            },
            function (err) {
                var msg = 'Unable to get location. ';
                if (err.code === 1) msg += 'Please allow location permission.';
                else if (err.code === 2) msg += 'Position unavailable.';
                else msg += 'Request timed out.';
                setGpsStatus(statusEl, msg, 'error');
                btn.textContent = 'Detect My Location';
                btn.disabled = false;
            },
            { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
        );
    });
}

function setGpsStatus(el, text, type) {
    if (!el) return;
    el.textContent = text;
    el.className = 'gps-status' + (type ? ' ' + type : '');
}

function reverseGeocode(lat, lng, addressInput) {
    if (!addressInput) return;
    var url = 'https://nominatim.openstreetmap.org/reverse?format=json&lat=' + lat + '&lon=' + lng;
    fetch(url, { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data && data.display_name) {
                addressInput.value = data.display_name;
            }
        })
        .catch(function () {});
}

function initImagePreview() {
    var input = document.getElementById('complaint-image');
    var preview = document.getElementById('image-preview');
    if (!input || !preview) return;

    input.addEventListener('change', function () {
        var file = input.files[0];
        if (!file) {
            preview.classList.remove('visible');
            preview.src = '';
            return;
        }
        var allowed = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];
        if (allowed.indexOf(file.type) === -1) {
            alert('Please upload JPG, PNG, GIF, or WEBP images only.');
            input.value = '';
            return;
        }
        if (file.size > 5 * 1024 * 1024) {
            alert('Image must be under 5MB.');
            input.value = '';
            return;
        }
        var reader = new FileReader();
        reader.onload = function (e) {
            preview.src = e.target.result;
            preview.classList.add('visible');
        };
        reader.readAsDataURL(file);
    });
}

var CATEGORY_KEYWORDS = {
    'Road Damage': ['pothole', 'road', 'crack', 'asphalt', 'pavement'],
    'Garbage': ['garbage', 'waste', 'trash', 'dump', 'litter'],
    'Water Leakage': ['water leak', 'pipe burst', 'tap', 'leakage'],
    'Drainage': ['drain', 'sewer', 'blockage', 'manhole'],
    'Streetlight Issues': ['streetlight', 'street light', 'lamp', 'dark']
};

function initAutoCategorize() {
    var titleEl = document.getElementById('complaint-title');
    var descEl = document.getElementById('complaint-description');
    var categoryEl = document.getElementById('complaint-category');
    var hintEl = document.getElementById('auto-cat-hint');
    if (!descEl || !categoryEl) return;

    function analyze() {
        var text = ((titleEl && titleEl.value) || '') + ' ' + (descEl.value || '');
        text = text.toLowerCase();
        var best = null;
        var bestScore = 0;
        Object.keys(CATEGORY_KEYWORDS).forEach(function (cat) {
            var score = 0;
            CATEGORY_KEYWORDS[cat].forEach(function (kw) {
                if (text.indexOf(kw) !== -1) score++;
            });
            if (score > bestScore) {
                bestScore = score;
                best = cat;
            }
        });
        if (best && bestScore > 0 && hintEl) {
            hintEl.textContent = 'Suggested category: ' + best + ' (auto-detected from description)';
            hintEl.classList.add('visible');
            if (!categoryEl.dataset.userChanged) {
                categoryEl.value = best;
            }
        } else if (hintEl) {
            hintEl.classList.remove('visible');
        }
    }

    categoryEl.addEventListener('change', function () {
        categoryEl.dataset.userChanged = '1';
    });

    if (titleEl) titleEl.addEventListener('input', debounce(analyze, 400));
    descEl.addEventListener('input', debounce(analyze, 400));
}

function debounce(fn, ms) {
    var t;
    return function () {
        clearTimeout(t);
        t = setTimeout(fn, ms);
    };
}

function initStatCounters() {
    document.querySelectorAll('[data-count]').forEach(function (el) {
        var target = parseInt(el.getAttribute('data-count'), 10) || 0;
        var duration = 1200;
        var start = 0;
        var startTime = null;
        function step(ts) {
            if (!startTime) startTime = ts;
            var progress = Math.min((ts - startTime) / duration, 1);
            el.textContent = Math.floor(progress * target);
            if (progress < 1) requestAnimationFrame(step);
            else el.textContent = target;
        }
        requestAnimationFrame(step);
    });
}

function initNotifDropdown() {
    var bell = document.getElementById('notif-bell');
    var dropdown = document.getElementById('notif-dropdown');
    if (!bell || !dropdown) return;
    bell.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        dropdown.classList.toggle('open');
    });
    document.addEventListener('click', function () {
        dropdown.classList.remove('open');
    });
}

function initHistoryFilters() {
    var search = document.getElementById('history-search');
    var table = document.getElementById('history-table');
    if (!search || !table) return;
    search.addEventListener('input', function () {
        var q = search.value.toLowerCase();
        table.querySelectorAll('tbody tr').forEach(function (row) {
            row.style.display = row.textContent.toLowerCase().indexOf(q) !== -1 ? '' : 'none';
        });
    });
}

function initLanguageSwitcher() {
    var sel = document.getElementById('lang-select');
    if (!sel) return;
    sel.addEventListener('change', function () {
        window.location.href = '/set-language/' + sel.value;
    });
}

function initVoiceInput() {
    var startBtn = document.getElementById('btn-voice-start');
    var stopBtn = document.getElementById('btn-voice-stop');
    var statusEl = document.getElementById('voice-status');
    var titleEl = document.getElementById('complaint-title');
    var descEl = document.getElementById('complaint-description');
    if (!startBtn || (!window.SpeechRecognition && !window.webkitSpeechRecognition)) {
        if (startBtn) startBtn.disabled = true;
        if (statusEl) statusEl.textContent = 'Voice input not supported in this browser.';
        return;
    }

    var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    var recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;

    var langMap = { en: 'en-IN', hi: 'hi-IN', kn: 'kn-IN', ta: 'ta-IN' };
    var htmlLang = document.documentElement.lang || 'en';
    recognition.lang = langMap[htmlLang] || 'en-IN';

    var finalTranscript = '';

    startBtn.addEventListener('click', function () {
        finalTranscript = '';
        recognition.start();
        startBtn.disabled = true;
        stopBtn.disabled = false;
        if (statusEl) statusEl.textContent = 'Listening... speak clearly.';
    });

    stopBtn.addEventListener('click', function () {
        recognition.stop();
        startBtn.disabled = false;
        stopBtn.disabled = true;
        if (statusEl) statusEl.textContent = 'Voice capture stopped.';
    });

    recognition.onresult = function (event) {
        var interim = '';
        for (var i = event.resultIndex; i < event.results.length; i++) {
            var t = event.results[i][0].transcript;
            if (event.results[i].isFinal) {
                finalTranscript += t + ' ';
            } else {
                interim += t;
            }
        }
        var combined = (finalTranscript + interim).trim();
        if (descEl) descEl.value = combined;
        if (titleEl && !titleEl.value && combined.length > 10) {
            titleEl.value = combined.substring(0, 80);
        }
        if (statusEl) statusEl.textContent = 'Captured: ' + combined.substring(0, 60) + (combined.length > 60 ? '...' : '');
    };

    recognition.onerror = function () {
        startBtn.disabled = false;
        stopBtn.disabled = true;
        if (statusEl) statusEl.textContent = 'Voice error. Try again or type manually.';
    };

    recognition.onend = function () {
        startBtn.disabled = false;
        stopBtn.disabled = true;
    };
}

function initImageDuplicateCheck() {
    var input = document.getElementById('complaint-image');
    var alertEl = document.getElementById('image-dup-alert');
    if (!input) return;

    input.addEventListener('change', function () {
        if (!input.files[0]) {
            if (alertEl) alertEl.style.display = 'none';
            return;
        }
        var fd = new FormData();
        fd.append('image', input.files[0]);
        fetch('/api/check-image-duplicate', { method: 'POST', body: fd })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!alertEl) return;
                if (data.duplicate) {
                    alertEl.style.display = 'block';
                    alertEl.textContent = data.message + ' (Complaint #' + data.complaint_id + ').';
                } else {
                    alertEl.style.display = 'none';
                }
            })
            .catch(function () {});
    });
}
