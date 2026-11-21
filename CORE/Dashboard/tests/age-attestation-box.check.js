// Node-runnable check for the GSEC FX.108 p1 age-attestation box (owner Q95=A)
// in Dashboard/pages/onboarding.html + Dashboard/js/auth.js.
//
// The hosted sign-up form must carry an age tick ("I confirm I am 16 years old
// or older") that is REQUIRED on hosted deploys and hidden + non-required on
// self-hosted ones (under-13 use is on-prem only, under a school's own
// authority). The API side enforces the same rule independently
// (services/signup_age_attestation.py), so this check only guards the FE wiring:
//   * the box exists, is a checkbox named `age_attestation` (the FormData name
//     the API reads) and sits inside the form,
//   * its label states the 16+ bar and never asks for a birth date (data
//     minimisation: the platform stores the BAND, not a birthday),
//   * loadOnboardingPlans hides + un-requires it when DEPLOYMENT_MODE != saas.
//
//   node --test CORE/Dashboard/tests/age-attestation-box.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');

const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'pages', 'onboarding.html'), 'utf8');
const auth = fs.readFileSync(path.join(__dirname, '..', 'js', 'auth.js'), 'utf8');

const box = html.match(/<input[^>]*id="age-attestation-checkbox"[^>]*>/);
assert.ok(box, 'age-attestation-checkbox missing from onboarding.html');
assert.ok(/name="age_attestation"/.test(box[0]),
  'the box must post the age_attestation field the API reads');
assert.ok(/type="checkbox"/.test(box[0]), 'the attestation is a tick, not a text field');
assert.ok(/\brequired\b/.test(box[0]), 'the tick is required on hosted deploys');
assert.ok(!/type="date"|birth|birthday/i.test(box[0]),
  'no birth date is collected: the platform stores the band only');

const label = html.slice(html.indexOf('age-attestation-checkbox'),
  html.indexOf('age-attestation-checkbox') + 900);
assert.ok(/16 years old or older/.test(label), 'the label names the 16+ bar');
assert.ok(/under 13/i.test(label), 'the label states under-13s are never hosted');

const plans = auth.slice(auth.indexOf('async function loadOnboardingPlans'));
const body = plans.slice(0, plans.indexOf('\n}', plans.indexOf('const isSaaS')));
assert.ok(/getElementById\('age-attestation-checkbox'\)/.test(body),
  'loadOnboardingPlans must reach the age box');
assert.ok(/ageSection\.style\.display = 'none'/.test(body),
  'on self-hosted deploys the age box is hidden');
assert.ok(/ageCheckbox\.removeAttribute\('required'\)/.test(body),
  'on self-hosted deploys the age box is not required');
