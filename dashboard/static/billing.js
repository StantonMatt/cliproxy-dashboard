'use strict';
// Calendar dates intentionally have no time zone: midnight UTC can display as
// the previous billing day in the user's browser.
const BillingDates = (() => {
  function calendarDay(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return null;
    const [year, month, day] = value.split('-').map(Number);
    const stamp = Date.UTC(year, month - 1, day);
    return new Date(stamp).toISOString().slice(0, 10) === value ? stamp : null;
  }
  function format(stamp, year = false) {
    return new Intl.DateTimeFormat('en-GB', {day:'numeric', month:'short', ...(year ? {year:'numeric'} : {}), timeZone:'UTC'}).format(stamp);
  }
  function describe(billing, now = new Date(), planType = null) {
    const day = calendarDay(billing?.renewal_date);
    if (day === null) return {label:'Billing renewal', date:'Not available', badge:'Confirm date', note:'Check the next payment date in ChatGPT.', urgent:false};
    const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
    const days = Math.round((day - today) / 86400000);
    if (planType === 'free' && days < 0) return {label:'Plan ended', date:format(day, true),
      badge:'Plan ended', note:'ChatGPT reports a Free plan.', urgent:false, ended:true};
    const confirmed = billing.confirmed === true;
    const result = {label:confirmed ? 'Billing renewal' : 'Expected renewal', date:format(day, true),
      badge:days < 0 ? 'Check date' : days === 0 ? 'Today' : days === 1 ? 'Tomorrow' : `In ${days} days`,
      urgent:days <= 7, note:''};
    if (days < 0) result.note = 'Date passed · check your current billing settings.';
    else if (!confirmed) result.note = 'Unconfirmed · based on account access period.';
    else result.note = `Review by ${format(day - 2 * 86400000)} · two days before renewal`;
    return result;
  }
  return {describe};
})();
if (typeof module !== 'undefined') module.exports = BillingDates;
