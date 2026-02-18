console.log('🚀 Script initializing...');

// ==========================================
// GLOBAL VARIABLES
// ==========================================
let currentDocType = null;
let extractedData = {};
let isProcessing = false;
let isMismatchAccepted = false;
let progressInterval = null;

// Prevent scrolling on body - Removed to allow natural scrolling
window.addEventListener('load', function() {
    console.log('Page loaded, scrolling enabled');
});

// ==========================================
// PAGE NAVIGATION
// ==========================================
function goHome() {
    currentDocType = null;
    switchPage('landingPage');
    resetScan();
}

function startScanning(docType) {
    console.log('startScanning called with:', docType);
    currentDocType = docType;
    const scanTitle = document.getElementById('scanTitle');
    if (scanTitle) {
        scanTitle.textContent = `Scanning: ${docType}`;
    }
    switchPage('scanningPage');
}

function switchPage(pageName) {
    console.log('switchPage called with:', pageName);
    const allPages = document.querySelectorAll('.page');
    allPages.forEach(page => page.classList.remove('active'));

    const targetPage = document.getElementById(pageName);
    if (targetPage) {
        targetPage.classList.add('active');
        if (pageName === 'scanningPage') {
            resetScan();
        }
    } else {
        console.error('Page not found:', pageName);
    }
}

// ==========================================
// FILE UPLOAD & PROCESSING (FIXED)
// ==========================================
function handleFileUpload(event) {
    const file = event.target.files[0];
    if (!file) return;

    console.log('File selected:', file.name, file.size);
    const inputElement = event.target;

    // FIX 1: IMMEDIATE UI FEEDBACK
    // This forces the bar to move BEFORE any processing starts
    isProcessing = true;
    updateStatus('Preparing document...');
    updateProgress(5);
    setProcessingLabel('Loading... <span class="progress-spinner">⏳</span>');

    // Display image preview
    const reader = new FileReader();
    reader.onload = (e) => {
        const preview = document.getElementById('imagePreview');
        const uploadArea = document.getElementById('uploadArea');
        if (preview && uploadArea) {
            preview.innerHTML = `<img src="${e.target.result}" alt="Document Preview" style="width: 100%; display: block;">`;
            preview.style.display = 'block';
            uploadArea.style.display = 'none';
        }

        // Start the fake 0-90% animation
        startAnimatedProgress();

        // FIX 2: Delay processing slightly to let UI render, then clear input
        setTimeout(() => {
            processDocument(file);
            inputElement.value = ''; // Safe to clear now
        }, 100);
    };
    reader.readAsDataURL(file);
}

function startAnimatedProgress() {
    console.log('Starting animated progress...');
    let percent = 5;
    clearInterval(progressInterval);

    // Ensure the spinner is visible
    const spinner = document.getElementById('progressSpinner');
    if (spinner) spinner.style.display = 'inline-block';

    // Animate from 5% up to 90%
    progressInterval = setInterval(() => {
        percent += Math.random() * 5 + 1;
        if (percent >= 90) {
            percent = 90; // Hold at 90 until server responds
        }
        updateProgress(percent);
        updateProgressPercent(Math.floor(percent));
    }, 400);
}

function stopAnimatedProgress(finalValue = 100) {
    clearInterval(progressInterval);
    updateProgress(finalValue);
    updateProgressPercent(finalValue);

    const label = finalValue === 100 ? 'Completed' : 'Stopped';
    setProcessingLabel(label);

    const spinner = document.getElementById('progressSpinner');
    if (spinner) spinner.style.display = 'none';
}

function setProcessingLabel(html) {
    const el = document.getElementById('processingLabel');
    if (el) el.innerHTML = html;
}

function updateProgressPercent(value) {
    const el = document.getElementById('progressPercent');
    if (el) el.textContent = value + '%';
}

function processDocument(file) {
    console.log('processDocument started for:', file.name);

    // Note: We don't check isProcessing here because we set it to true in handleFileUpload
    const formData = new FormData();
    formData.append('file', file);
    formData.append('doc_type', currentDocType || 'Unknown');

    // Update UI text
    updateStatus('Uploading and analyzing document...');

    fetch('api/upload', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        if (!response.ok) {
            return response.json().then(err => { throw new Error(err.detail || 'Server Error ' + response.status); });
        }
        return response.json();
    })
    .then(data => {
        console.log('API Response:', data);

        // --- MISMATCH HANDLING ---
        if ((data.success === false && data.error && data.error.toLowerCase().includes('mismatch')) ||
            (data.doc_type && currentDocType && data.doc_type !== currentDocType && data.doc_type !== 'Unknown_Document')) {

            stopAnimatedProgress(0); // Stop animation
            showAnimatedMismatch(data.doc_type || 'Unknown', currentDocType, data.data || {});
            updateStatus('⚠️ Document mismatch detected');
            return;
        }

        // --- SUCCESS ---
        stopAnimatedProgress(100); // Snap to 100%
        if (data.success && data.data && Object.keys(data.data).length > 0 && Object.values(data.data).some(Boolean)) {
            mergeExtractedData(data.data);
            showAnimatedResults();
            updateStatus('✓ Scan complete');
            showToast('Document scanned successfully!', 'success');
        } else {
            updateStatus('✗ No details found.');
            showToast(data.error || 'No details extracted.', 'error');
            clearResults();
        }
    })
    .catch(error => {
        console.error('Upload Error:', error);
        stopAnimatedProgress(0);
        updateStatus('✗ Error: ' + error.message);
        showToast('Error: ' + error.message, 'error');
        clearResults();
    })
    .finally(() => {
        isProcessing = false;
    });
}

// ==========================================
// RESULTS & MISMATCH DISPLAY
// ==========================================
function showAnimatedMismatch(detected, expected, extracted) {
    const resultsTable = document.getElementById('resultsTable');
    if (!resultsTable) return;

    // This HTML replaces the content of the "Extracted Details" box
    let html = `
        <div class="mismatch-error-animated">
            <div style="font-size: 3rem; margin-bottom: 0.5rem;">⚠️</div>
            <h3 style="color: #c53030; margin-bottom: 0.5rem; font-size: 1.2rem;">Document Mismatch</h3>
            <p style="color: #742a2a; margin-bottom: 1rem; font-size: 0.9rem;">
                Expected: <strong>${expected}</strong><br>
                Detected: <strong>${detected}</strong>
            </p>
            <div style="display: flex; gap: 0.5rem; justify-content: center;">
                <button class="btn btn-secondary" onclick="goHome()" style="font-size:0.8rem; padding: 0.4rem 0.8rem;">Select Correct Type</button>
                <button class="btn btn-primary" onclick="continueMismatch()" style="font-size:0.8rem; padding: 0.4rem 0.8rem;">Continue</button>
            </div>
        </div>
    `;

    resultsTable.innerHTML = html;

    // Ensure the "Add Details" button is hidden when there is an error
    const addDetailsBtn = document.getElementById('addDetailsBtn');
    if (addDetailsBtn) addDetailsBtn.style.display = 'none';

    // Hide inline warning if it exists
    const inlineWarning = document.getElementById('mismatchWarning');
    if (inlineWarning) inlineWarning.style.display = 'none';
}

function showAnimatedResults() {
    displayResults();
    const resultsTable = document.getElementById('resultsTable');
    if (resultsTable) {
        resultsTable.classList.add('results-animated');
        setTimeout(() => resultsTable.classList.remove('results-animated'), 1200);
    }
}

function mergeExtractedData(newData) {
    if (!extractedData || Object.keys(extractedData).length === 0) {
        extractedData = { ...newData };
    } else {
        extractedData = { ...extractedData, ...newData };
    }
}

function displayResults() {
    console.log('displayResults called with data:', extractedData);
    const resultsTable = document.getElementById('resultsTable');
    const addDetailsBtn = document.getElementById('addDetailsBtn');

    if (!extractedData || Object.keys(extractedData).length === 0) {
        resultsTable.innerHTML = '<div class="empty-state"><p>No data extracted</p></div>';
        if (addDetailsBtn) addDetailsBtn.style.display = 'flex';
        return;
    }

    let html = '';
    for (const [key, value] of Object.entries(extractedData)) {
        if (value) {
            html += `
                <div class="result-item">
                    <div class="result-icon">📄</div>
                    <div class="result-info">
                        <div class="result-key">${key}</div>
                        <div class="result-value" 
                             contenteditable="true" 
                             oninput="updateExtractedValue('${key}', this.textContent)"
                             title="Edit directly">${escapeHtml(value)}</div>
                    </div>
                    <button class="edit-field-btn" onclick="editField('${key}')" title="Focus field" style="opacity: 0.5; border: none; background: none; cursor: pointer;">✏️</button>
                </div>
            `;
        }
    }
    resultsTable.innerHTML = html;
    if (addDetailsBtn) addDetailsBtn.style.display = 'flex';
}

function clearResults() {
    extractedData = {};
    const resultsTable = document.getElementById('resultsTable');
    if (resultsTable) resultsTable.innerHTML = '<div class="empty-state"><p>No data extracted</p></div>';
    const addBtn = document.getElementById('addDetailsBtn');
    if (addBtn) addBtn.style.display = 'none';
}

// ==========================================
// EDIT MODAL & ACTIONS
// ==========================================
function openEditModal() {
    const modal = document.getElementById('editModal');
    if (!modal) return;

    document.getElementById('idNumber').value = extractedData['ID Number'] || '';
    document.getElementById('name').value = extractedData['Name'] || '';
    document.getElementById('dob').value = extractedData['DOB'] || '';
    document.getElementById('gender').value = extractedData['Gender'] || '';
    document.getElementById('vidNumber').value = extractedData['VID Number'] || '';
    document.getElementById('parentName').value = extractedData['Parent Name'] || '';
    document.getElementById('address').value = extractedData['Address'] || '';
    document.getElementById('validity').value = extractedData['Validity'] || '';

    modal.classList.add('active');
}

function closeEditModal() {
    document.getElementById('editModal').classList.remove('active');
}

function continueMismatch() {
    isMismatchAccepted = true;
    displayResults();
    updateStatus('✓ Continuing with detected type');
    showToast('Continuing with detected document type', 'success');
}

function updateExtractedValue(key, value) {
    extractedData[key] = value.trim();
}

function editField(fieldName) {
    const resultsTable = document.getElementById('resultsTable');
    const items = resultsTable.querySelectorAll('.result-item');
    for (const item of items) {
        const key = item.querySelector('.result-key').textContent;
        if (key === fieldName) {
            const valueDiv = item.querySelector('.result-value');
            if (valueDiv) {
                valueDiv.focus();
                // move cursor to end
                const range = document.createRange();
                const sel = window.getSelection();
                range.selectNodeContents(valueDiv);
                range.collapse(false);
                sel.removeAllRanges();
                sel.addRange(range);
            }
            return;
        }
    }
}

function saveEditedData() {
    const newData = {
        'ID Number': document.getElementById('idNumber').value.trim(),
        'Name': document.getElementById('name').value.trim(),
        'DOB': document.getElementById('dob').value.trim(),
        'Gender': document.getElementById('gender').value.trim(),
        'VID Number': document.getElementById('vidNumber').value.trim(),
        'Parent Name': document.getElementById('parentName').value.trim(),
        'Address': document.getElementById('address').value.trim(),
        'Validity': document.getElementById('validity').value.trim()
    };

    for (const key in newData) {
        if (!newData[key]) delete newData[key];
    }

    extractedData = { ...extractedData, ...newData };
    closeEditModal();
    displayResults();
    showToast('Details updated successfully!', 'success');
    saveData();
}

function saveData() {
    if (!extractedData || Object.keys(extractedData).length === 0) {
        showToast('No data to save', 'error');
        return;
    }

    const payload = {
        data: extractedData,
        doc_type: currentDocType || 'GENERAL'
    };

    updateStatus('Saving data...');

    fetch('/api/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast(data.message || 'Data saved successfully!', 'success');
            updateStatus('✓ ' + (data.message || 'Data saved'));
        } else {
            showToast(data.error || 'Failed to save data', 'error');
            updateStatus('✗ Save failed');
        }
    })
    .catch(error => {
        showToast('Error: ' + error.message, 'error');
        updateStatus('✗ Save error');
    });
}

function resetScan() {
    console.log('Resetting scan...');
    extractedData = {};
    isMismatchAccepted = false;
    isProcessing = false;
    clearInterval(progressInterval);

    const fileInput = document.getElementById('fileInput');
    if (fileInput) fileInput.value = '';

    const preview = document.getElementById('imagePreview');
    const uploadArea = document.getElementById('uploadArea');
    if (preview && uploadArea) {
        preview.innerHTML = '';
        preview.style.display = 'none';
        uploadArea.style.display = 'flex';
    }

    clearResults();
    updateProgress(0);
    updateStatus('Ready to scan');
    setProcessingLabel('Ready to scan');
}

// ==========================================
// UI HELPERS
// ==========================================
function updateProgress(value) {
    const el = document.getElementById('progressFill');
    if (el) {
        el.style.width = value + '%';
        el.style.backgroundSize = '200% 100%';
    }
}

function updateStatus(text) {
    const el = document.getElementById('statusText');
    if (el) el.textContent = text;
}

function showToast(message, type = 'success') {
    const toast = document.getElementById(type === 'success' ? 'successToast' : 'errorToast');
    const messageElement = type === 'success' ? document.getElementById('toastMessage') : document.getElementById('errorMessage');

    if (messageElement) messageElement.textContent = message;
    if (toast) {
        toast.classList.add('show');
        setTimeout(() => { toast.classList.remove('show'); }, 3000);
    }
}

function hideToast() {
    document.querySelectorAll('.toast').forEach(toast => toast.classList.remove('show'));
}

function escapeHtml(text) {
    if (!text) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return text.toString().replace(/[&<>"']/g, m => map[m]);
}

// ==========================================
// INITIALIZATION
// ==========================================
document.addEventListener('DOMContentLoaded', () => {
    console.log('✓ Application loaded successfully');

    const modal = document.getElementById('editModal');
    if (modal) {
        document.addEventListener('click', (event) => {
            if (event.target === modal) closeEditModal();
        });
    }

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeEditModal();
    });
});