-- phpMyAdmin SQL Dump
-- version 5.2.1
-- https://www.phpmyadmin.net/
--
-- Host: 127.0.0.1
-- Generation Time: Sep 27, 2026 at 11:33 AM
-- Server version: 10.4.32-MariaDB
-- PHP Version: 8.2.12

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";


/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;


-- --------------------------------------------------------

--
-- Table structure for table `medicines`
--

CREATE TABLE `medicines` (
  `id` int(11) NOT NULL,
  `brand_name` varchar(255) DEFAULT NULL,
  `generic_name` varchar(255) DEFAULT NULL,
  `strength` varchar(255) DEFAULT NULL,
  `dosage_form` varchar(255) DEFAULT NULL,
  `company_name` varchar(255) DEFAULT NULL,
  `unit_price` varchar(50) DEFAULT NULL,
  `strip_price` varchar(50) DEFAULT NULL,
  `pack_size` varchar(100) DEFAULT NULL,
  `image_url` varchar(500) DEFAULT NULL,
  `source_url` varchar(500) DEFAULT NULL,
  `indications` text DEFAULT NULL,
  `pharmacology` text DEFAULT NULL,
  `dosage_and_administration` text DEFAULT NULL,
  `interaction` text DEFAULT NULL,
  `contraindications` text DEFAULT NULL,
  `side_effects` text DEFAULT NULL,
  `pregnancy_and_lactation` text DEFAULT NULL,
  `precautions_and_warnings` text DEFAULT NULL,
  `use_in_special_populations` text DEFAULT NULL,
  `overdose_effects` text DEFAULT NULL,
  `therapeutic_class` text DEFAULT NULL,
  `storage_conditions` text DEFAULT NULL,
  `common_questions` text DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT current_timestamp()
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

--
-- Indexes for dumped tables
--

--
-- Indexes for table `medicines`
--
ALTER TABLE `medicines`
  ADD PRIMARY KEY (`id`);

--
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `medicines`
--
ALTER TABLE `medicines`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT;
COMMIT;

/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
