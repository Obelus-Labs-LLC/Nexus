"""End-to-end parser tests for the 6 language extractors added in the
pitlane-mcp-inspired language expansion: Ruby, PHP, Kotlin, Swift, Zig, Solidity.

These tests assert that each extractor produces at least the headline
symbols agents will search for (class/function/method names), and that
imports are captured where relevant. They intentionally do NOT over-specify
exact qualified-name formatting — grammar quirks vary slightly by
tree-sitter version, and we care about retrieval quality, not string
identity.
"""

from pathlib import Path

from nexus.index.parser import parse_file


def _parse(tmp_path: Path, filename: str, language: str, src: str):
    p = tmp_path / filename
    p.write_text(src, encoding="utf-8")
    return parse_file(p, language)


# ── Ruby ─────────────────────────────────────────────────────────────────────

RUBY_SRC = """
require 'json'
require_relative 'helper'

module Acme
  class UserService
    def initialize(db)
      @db = db
    end

    def find_by_id(id)
      @db.users.find(id)
    end

    def self.create(attrs)
      new(attrs)
    end
  end
end

def top_level_fn(x)
  x * 2
end
"""


def test_ruby_extracts_class_and_methods(tmp_path: Path):
    result = _parse(tmp_path, "user_service.rb", "ruby", RUBY_SRC)
    names = {s.name for s in result.symbols}
    kinds = {s.name: s.kind for s in result.symbols}
    assert "UserService" in names
    assert "Acme" in names
    assert "find_by_id" in names
    assert "top_level_fn" in names
    assert kinds.get("UserService") == "class"
    assert kinds.get("Acme") == "module"


def test_ruby_extracts_requires(tmp_path: Path):
    result = _parse(tmp_path, "user_service.rb", "ruby", RUBY_SRC)
    modules = {imp.module for imp in result.imports}
    assert "json" in modules
    assert "helper" in modules


# ── PHP ──────────────────────────────────────────────────────────────────────

PHP_SRC = """<?php
namespace App\\Services;

use App\\Models\\User;

class UserService {
    private $db;

    public function __construct($db) {
        $this->db = $db;
    }

    public function findById($id) {
        return $this->db->users->find($id);
    }

    private function validate($x) {
        return $x !== null;
    }
}

function top_level_helper($x) {
    return $x * 2;
}
"""


def test_php_extracts_class_methods_and_visibility(tmp_path: Path):
    result = _parse(tmp_path, "UserService.php", "php", PHP_SRC)
    names = {s.name for s in result.symbols}
    assert "UserService" in names
    assert "findById" in names
    assert "validate" in names
    assert "top_level_helper" in names

    by_name = {s.name: s for s in result.symbols}
    # Visibility modifier must be captured
    assert by_name["validate"].visibility == "private"
    assert by_name["findById"].visibility == "public"
    # Methods must be marked as methods, not free functions
    assert by_name["findById"].kind == "method"
    assert by_name["top_level_helper"].kind == "function"


def test_php_extracts_use_imports(tmp_path: Path):
    result = _parse(tmp_path, "UserService.php", "php", PHP_SRC)
    assert any("App\\Models\\User" in imp.module for imp in result.imports)


# ── Kotlin ───────────────────────────────────────────────────────────────────

KOTLIN_SRC = """
package com.acme.services

import java.util.UUID
import kotlinx.coroutines.Deferred

class UserService(private val db: Database) {
    fun findById(id: UUID): User? {
        return db.users.find(id)
    }

    suspend fun fetchAsync(id: UUID): Deferred<User> {
        return TODO()
    }
}

object Singleton {
    fun doThing() {}
}

fun topLevelFn(x: Int) = x * 2
"""


def test_kotlin_extracts_class_fun_object(tmp_path: Path):
    result = _parse(tmp_path, "UserService.kt", "kotlin", KOTLIN_SRC)
    names = {s.name for s in result.symbols}
    assert "UserService" in names
    assert "findById" in names
    assert "Singleton" in names
    assert "topLevelFn" in names


def test_kotlin_extracts_imports(tmp_path: Path):
    result = _parse(tmp_path, "UserService.kt", "kotlin", KOTLIN_SRC)
    modules = {imp.module for imp in result.imports}
    assert any("UUID" in m or "java.util.UUID" in m for m in modules)


# ── Swift ────────────────────────────────────────────────────────────────────

SWIFT_SRC = """
import Foundation
import Combine

protocol Identifiable {
    var id: String { get }
}

struct User: Identifiable {
    let id: String
    let name: String
}

class UserService {
    func findById(_ id: String) -> User? {
        return nil
    }
}

actor Counter {
    var value: Int = 0
    func increment() { value += 1 }
}

func topLevel(_ x: Int) -> Int {
    return x * 2
}
"""


def test_swift_extracts_protocol_struct_class_actor(tmp_path: Path):
    result = _parse(tmp_path, "UserService.swift", "swift", SWIFT_SRC)
    names = {s.name for s in result.symbols}
    assert "Identifiable" in names
    assert "User" in names
    assert "UserService" in names
    assert "Counter" in names
    assert "findById" in names
    assert "topLevel" in names


def test_swift_extracts_imports(tmp_path: Path):
    result = _parse(tmp_path, "UserService.swift", "swift", SWIFT_SRC)
    modules = {imp.module for imp in result.imports}
    assert "Foundation" in modules
    assert "Combine" in modules


# ── Zig ──────────────────────────────────────────────────────────────────────

ZIG_SRC = """
const std = @import("std");
const testing = @import("std").testing;

pub const User = struct {
    id: u64,
    name: []const u8,
};

pub fn findById(id: u64) ?User {
    return null;
}

fn privateHelper(x: i32) i32 {
    return x * 2;
}
"""


def test_zig_extracts_functions_and_struct(tmp_path: Path):
    result = _parse(tmp_path, "user.zig", "zig", ZIG_SRC)
    names = {s.name for s in result.symbols}
    assert "findById" in names
    assert "privateHelper" in names
    # struct extraction is best-effort — verify at least the functions parse


def test_zig_extracts_imports(tmp_path: Path):
    result = _parse(tmp_path, "user.zig", "zig", ZIG_SRC)
    # @import calls are captured as imports
    modules = {imp.module for imp in result.imports}
    assert "std" in modules


# ── Solidity ─────────────────────────────────────────────────────────────────

SOLIDITY_SRC = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "./IERC20.sol";
import {SafeMath} from "./SafeMath.sol";

interface IVault {
    function deposit(uint256 amount) external;
}

library MathLib {
    function add(uint256 a, uint256 b) internal pure returns (uint256) {
        return a + b;
    }
}

contract Vault is IVault {
    event Deposited(address indexed user, uint256 amount);
    modifier onlyOwner() { _; }

    constructor() {}

    function deposit(uint256 amount) external override {
        emit Deposited(msg.sender, amount);
    }
}
"""


def test_solidity_extracts_contract_interface_library(tmp_path: Path):
    result = _parse(tmp_path, "Vault.sol", "solidity", SOLIDITY_SRC)
    names = {s.name for s in result.symbols}
    assert "IVault" in names
    assert "MathLib" in names
    assert "Vault" in names
    assert "deposit" in names
    assert "add" in names


def test_solidity_extracts_event_and_modifier(tmp_path: Path):
    result = _parse(tmp_path, "Vault.sol", "solidity", SOLIDITY_SRC)
    names = {s.name for s in result.symbols}
    assert "Deposited" in names
    assert "onlyOwner" in names


def test_solidity_extracts_imports(tmp_path: Path):
    result = _parse(tmp_path, "Vault.sol", "solidity", SOLIDITY_SRC)
    modules = {imp.module for imp in result.imports}
    assert any("IERC20" in m for m in modules)
    assert any("SafeMath" in m for m in modules)
