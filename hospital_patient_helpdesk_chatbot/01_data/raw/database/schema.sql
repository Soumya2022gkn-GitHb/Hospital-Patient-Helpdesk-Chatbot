-- Synthetic helpdesk database schema
CREATE TABLE departments (name TEXT PRIMARY KEY, location TEXT, phone TEXT, hours TEXT, services TEXT);
CREATE TABLE doctor_schedule (doctor_name TEXT, department TEXT, day TEXT, start_time TEXT, end_time TEXT, location TEXT);
CREATE TABLE portal_support_topics (topic TEXT PRIMARY KEY, guidance TEXT, escalation_contact TEXT);
