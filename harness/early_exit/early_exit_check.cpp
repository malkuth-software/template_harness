// early_exit_check: the early-exit integrity check (docs/early_exit_check.md),
// in STXlib's vocabulary (audit E, section 1: marks, site markers, exit
// classes, rules R1 to R10, patterns P1 to P13).
//
//   early_exit_check --config <settings.json> [-p <build dir>] <source>... [-- <compiler arguments>]
//
// Reads each translation unit with clang, walks every first-party function
// and writes one JSON object per entry to standard output, one per line:
//
//   {"rule":"R5","level":"finding","pattern":"P4","file":"/abs/path.cpp","line":12,
//    "column":5,"function":"ns::Type::method","message":"..."}
//
// `level` is "finding" (a rule broken) or "report" (listed for review, never
// a failure). The settings file is written by harness/check_early_exit.py
// from the project file's [early_exit] table:
//
//   {"first_party_roots": [...], "foreign_paths": [...], "standard_library_foreign": false,
//    "foreign_functions": [...], "no_exit_functions": [...], "cancellation_points": [...],
//    "allocator_allowlist": [...], "release_functions": [...]}
//
// the last five being regular expressions over qualified names. Exit
// status: 0 when every unit was read (entries or not), 1 when a unit failed
// to parse or the settings are unusable.
//
// One function body is read in one walk (BodyWalker), in source order,
// keeping the live scope objects (guards, locks, open invariants,
// no-cancel scopes) and the pending site marker. It records the facts the
// rules compare: foreign calls with their exit class and what was live or
// marked at each, writes and saves of state, pair halves, releases and
// let-gos, and the no-unwind facts. FunctionChecker then applies the
// rules. R7 is read from variable declarations, and R6's declaration half
// from function declarations.

#include "clang/AST/ASTConsumer.h"
#include "clang/AST/ASTContext.h"
#include "clang/AST/Attr.h"
#include "clang/AST/Decl.h"
#include "clang/AST/DeclCXX.h"
#include "clang/AST/DeclObjC.h"
#include "clang/AST/DeclTemplate.h"
#include "clang/AST/Expr.h"
#include "clang/AST/ExprCXX.h"
#include "clang/AST/ExprObjC.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/AST/Stmt.h"
#include "clang/AST/StmtCXX.h"
#include "clang/AST/Type.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendAction.h"
#include "clang/Lex/Lexer.h"
#include "clang/Tooling/CommonOptionsParser.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Error.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Regex.h"
#include "llvm/Support/raw_ostream.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace early_exit {

// --- the vocabulary (STXlib audit E, section 1) -------------------------------------------

constexpr std::string_view mark_foreign = "early_exit.foreign";
constexpr std::string_view mark_cancel = "early_exit.cancel";
constexpr std::string_view mark_no_exit = "early_exit.no_exit";
constexpr std::string_view mark_own = "early_exit.own";
constexpr std::string_view mark_no_unwind = "early_exit.no_unwind";
constexpr std::string_view mark_guard = "early_exit.guard";
constexpr std::string_view mark_lock = "early_exit.lock";
constexpr std::string_view mark_open = "early_exit.open";
constexpr std::string_view mark_no_cancel = "early_exit.no_cancel";
constexpr std::string_view mark_whole = "early_exit.whole";
constexpr std::string_view mark_forced_unwind = "early_exit.forced_unwind";

// The site markers, matched by the last two components of the type's
// qualified name.
constexpr std::string_view marker_space = "early_exit";
constexpr std::string_view marker_whole = "Whole";
constexpr std::string_view marker_guarded = "Guarded";
constexpr std::string_view marker_no_exit = "NoExit";

// A NoExit reason beginning with this is an owner-accepted terminate (P13).
constexpr std::string_view accepted_terminate = "terminate accepted";

// --- settings -------------------------------------------------------------------------

using Patterns = std::vector<std::unique_ptr<llvm::Regex>>;

struct Settings final {
    std::vector<std::string> first_party_roots{};
    std::vector<std::string> foreign_paths{};
    bool standard_library_foreign{};
    Patterns foreign_functions{};
    Patterns no_exit_functions{};
    Patterns cancellation_points{};
    Patterns allocator_allowlist{};
    Patterns release_functions{};
};

// Appends each string of `object[key]` to `out`; false when the member is
// missing or not an array of strings.
bool read_strings(const llvm::json::Object& object, llvm::StringRef key, std::vector<std::string>& out) {
    const llvm::json::Array* array = object.getArray(key);
    if (array == nullptr) return false;
    for (const llvm::json::Value& value : *array) {
        const std::optional<llvm::StringRef> text = value.getAsString();
        if (!text.has_value()) return false;
        out.push_back((*text).str());
    }
    return true;
}

// Compiles the strings of `object[key]` into `out`; false, naming the
// member or the pattern on standard error, when one is missing or invalid.
bool read_patterns(const llvm::json::Object& object, llvm::StringRef key, Patterns& out) {
    std::vector<std::string> texts{};
    if (!read_strings(object, key, texts)) {
        llvm::errs() << "early_exit_check: settings lack the list " << key << "\n";
        return false;
    }
    for (const std::string& text : texts) {
        std::unique_ptr<llvm::Regex> compiled = std::make_unique<llvm::Regex>(text);
        std::string error{};
        if (!(*compiled).isValid(error)) {
            llvm::errs() << "early_exit_check: " << key << ": invalid pattern '" << text << "': " << error << "\n";
            return false;
        }
        out.push_back(std::move(compiled));
    }
    return true;
}

// Reads the settings file into `settings`, validating it whole first;
// `settings` is left untouched on failure.
bool read_settings(llvm::StringRef path, Settings& settings) {
    llvm::ErrorOr<std::unique_ptr<llvm::MemoryBuffer>> buffer = llvm::MemoryBuffer::getFile(path);
    if (!buffer) {
        llvm::errs() << "early_exit_check: cannot read " << path << "\n";
        return false;
    }
    llvm::Expected<llvm::json::Value> parsed = llvm::json::parse((**buffer).getBuffer());
    if (!parsed) {
        llvm::errs() << "early_exit_check: " << path << ": " << llvm::toString(parsed.takeError()) << "\n";
        return false;
    }
    const llvm::json::Object* object = (*parsed).getAsObject();
    if (object == nullptr) {
        llvm::errs() << "early_exit_check: " << path << " is not a JSON object\n";
        return false;
    }
    Settings read{};
    const bool roots = read_strings(*object, "first_party_roots", read.first_party_roots) &&
                       read_strings(*object, "foreign_paths", read.foreign_paths);
    if (!roots) {
        llvm::errs() << "early_exit_check: " << path << " lacks first_party_roots or foreign_paths\n";
        return false;
    }
    const std::optional<bool> standard = (*object).getBoolean("standard_library_foreign");
    if (!standard.has_value()) {
        llvm::errs() << "early_exit_check: " << path << " lacks standard_library_foreign\n";
        return false;
    }
    read.standard_library_foreign = *standard;
    const bool patterns = read_patterns(*object, "foreign_functions", read.foreign_functions) &&
                          read_patterns(*object, "no_exit_functions", read.no_exit_functions) &&
                          read_patterns(*object, "cancellation_points", read.cancellation_points) &&
                          read_patterns(*object, "allocator_allowlist", read.allocator_allowlist) &&
                          read_patterns(*object, "release_functions", read.release_functions);
    if (!patterns) return false;
    settings = std::move(read);
    return true;
}

bool matches_any(const Patterns& patterns, llvm::StringRef text) {
    for (const std::unique_ptr<llvm::Regex>& pattern : patterns) {
        if ((*pattern).match(text)) return true;
    }
    return false;
}

// Whether `path` is `root` or lies under it (both absolute, with forward
// slashes).
bool under(llvm::StringRef path, llvm::StringRef root) {
    if (!path.starts_with(root)) return false;
    if (path.size() == root.size() || root.ends_with("/")) return true;
    const bool separated = path[root.size()] == '/';
    return separated;
}

bool under_any(llvm::StringRef path, const std::vector<std::string>& roots) {
    for (const std::string& root : roots) {
        if (under(path, root)) return true;
    }
    return false;
}

// --- entries ----------------------------------------------------------------------------

enum class Level : std::uint8_t { finding, report };

struct Entry final {
    std::string rule{};
    Level level{Level::finding};
    std::string pattern{};
    std::string file{};
    unsigned line{};
    unsigned column{};
    std::string function{};
    std::string message{};

    friend bool operator<(const Entry& left, const Entry& right) {
        if (left.file != right.file) return left.file < right.file;
        if (left.line != right.line) return left.line < right.line;
        if (left.column != right.column) return left.column < right.column;
        if (left.rule != right.rule) return left.rule < right.rule;
        return left.message < right.message;
    }
};

// Every entry of the run, kept once: a header's entry repeats in each unit
// that includes it, and a template's in each instantiation.
class EntrySet final {
public:
    void add(Entry entry) { entries_.insert(std::move(entry)); }

    void write(llvm::raw_ostream& out) const {
        for (const Entry& entry : entries_) {
            llvm::json::Object record{};
            record["rule"] = entry.rule;
            record["level"] = entry.level == Level::finding ? "finding" : "report";
            record["pattern"] = entry.pattern;
            record["file"] = entry.file;
            record["line"] = static_cast<std::int64_t>(entry.line);
            record["column"] = static_cast<std::int64_t>(entry.column);
            record["function"] = entry.function;
            record["message"] = entry.message;
            const llvm::json::Value value(std::move(record));
            out << value << "\n";
        }
    }

private:
    std::set<Entry> entries_{};
};

// --- marks, names and types -------------------------------------------------------------

bool has_mark(const clang::Decl& declaration, std::string_view mark) {
    const llvm::StringRef wanted(mark.data(), mark.size());
    for (const clang::AnnotateAttr* attribute : declaration.specific_attrs<clang::AnnotateAttr>()) {
        if ((*attribute).getAnnotation() == wanted) return true;
    }
    return false;
}

// Whether any declaration of `function`, or of its template pattern,
// carries `mark`.
bool function_marked(const clang::FunctionDecl& function, std::string_view mark) {
    for (const clang::FunctionDecl* redeclaration : function.redecls()) {
        if (has_mark(*redeclaration, mark)) return true;
    }
    const clang::FunctionDecl* pattern = function.getTemplateInstantiationPattern();
    if (pattern != nullptr && pattern != &function) {
        for (const clang::FunctionDecl* redeclaration : (*pattern).redecls()) {
            if (has_mark(*redeclaration, mark)) return true;
        }
    }
    return false;
}

// Whether `method` or a method it overrides, at any depth, carries `mark`.
// Walked with an explicit work list.
bool method_marked(const clang::CXXMethodDecl& method, std::string_view mark) {
    std::vector<const clang::CXXMethodDecl*> pending{};
    pending.push_back(&method);
    while (!pending.empty()) {
        const clang::CXXMethodDecl* current = pending.back();
        pending.pop_back();
        if (function_marked(*current, mark)) return true;
        for (const clang::CXXMethodDecl* overridden : (*current).overridden_methods()) pending.push_back(overridden);
    }
    return false;
}

// The record a class template specialization was made from, or the record
// itself: marks are written on the pattern.
const clang::CXXRecordDecl& record_pattern(const clang::CXXRecordDecl& record) {
    const clang::CXXRecordDecl* pattern = record.getTemplateInstantiationPattern();
    if (pattern != nullptr) return *pattern;
    return record;
}

// Whether any declaration of `record`'s pattern carries `mark`.
bool record_marked(const clang::CXXRecordDecl& record, std::string_view mark) {
    const clang::CXXRecordDecl& pattern = record_pattern(record);
    for (const clang::TagDecl* redeclaration : pattern.redecls()) {
        if (has_mark(*redeclaration, mark)) return true;
    }
    return false;
}

// The name the settings' patterns match: the qualified name, with a class
// template specialization's arguments dropped (`std::function::operator()`).
std::string qualified_name(const clang::FunctionDecl& function) {
    const clang::CXXMethodDecl* method = llvm::dyn_cast<clang::CXXMethodDecl>(&function);
    if (method != nullptr) {
        const clang::ClassTemplateSpecializationDecl* specialization =
            llvm::dyn_cast<clang::ClassTemplateSpecializationDecl>((*method).getParent());
        if (specialization != nullptr) {
            const clang::ClassTemplateDecl* made_from = (*specialization).getSpecializedTemplate();
            std::string name = (*made_from).getQualifiedNameAsString() + "::" + (*method).getNameAsString();
            return name;
        }
    }
    std::string name = function.getQualifiedNameAsString();
    return name;
}

std::string record_name(const clang::CXXRecordDecl& record) {
    std::string name = record_pattern(record).getQualifiedNameAsString();
    return name;
}

// The record a variable of `type` holds (references and arrays not), or null.
const clang::CXXRecordDecl* record_of(clang::QualType type) {
    if ((*type).isReferenceType() || (*type).isPointerType()) return nullptr;
    const clang::CXXRecordDecl* record = (*type).getAsCXXRecordDecl();
    return record;
}

// Whether a site marker's type is `early_exit::<kind>` by its last two name
// components.
bool marker_type(const clang::CXXRecordDecl& record, std::string_view kind) {
    if (record.getName() != llvm::StringRef(kind.data(), kind.size())) return false;
    const clang::NamespaceDecl* space = llvm::dyn_cast<clang::NamespaceDecl>(record.getDeclContext());
    if (space == nullptr) return false;
    const bool named = (*space).getName() == llvm::StringRef(marker_space.data(), marker_space.size());
    return named;
}

// Whether calling something of `type` (a function type, or a pointer,
// reference or member pointer to one) cannot throw.
bool callee_type_nothrow(clang::QualType type) {
    clang::QualType function_type = type;
    if ((*function_type).isPointerType() || (*function_type).isReferenceType()) {
        function_type = (*function_type).getPointeeType();
    }
    const clang::MemberPointerType* member = (*function_type).getAs<clang::MemberPointerType>();
    if (member != nullptr) function_type = (*member).getPointeeType();
    const clang::FunctionProtoType* prototype = (*function_type).getAs<clang::FunctionProtoType>();
    if (prototype == nullptr) return false;
    const bool nothrow = (*prototype).isNothrow();
    return nothrow;
}

bool function_nothrow(const clang::FunctionDecl& function) {
    if (function.hasAttr<clang::NoThrowAttr>()) return true;
    const clang::FunctionProtoType* prototype = (*function.getType()).getAs<clang::FunctionProtoType>();
    if (prototype == nullptr) return false;
    const bool nothrow = (*prototype).isNothrow();
    return nothrow;
}

// Whether `function` takes a callable: a function pointer, function
// reference, member function pointer, or a std::function-like record.
bool takes_callable(const clang::FunctionDecl& function) {
    for (const clang::ParmVarDecl* parameter : function.parameters()) {
        const clang::QualType type = (*(*parameter).getType()).isReferenceType()
                                         ? (*(*parameter).getType()).getPointeeType()
                                         : (*parameter).getType();
        if ((*type).isFunctionPointerType() || (*type).isFunctionType() ||
            (*type).isMemberFunctionPointerType()) {
            return true;
        }
        const clang::CXXRecordDecl* record = (*type).getAsCXXRecordDecl();
        if (record != nullptr && (*record).getName().ends_with("function")) return true;
    }
    return false;
}

// Whether `function` belongs to the C++ standard library or its
// implementation namespaces (`std`, `__gnu_cxx`, `__cxxabiv1`).
bool in_standard_library(const clang::Decl& declaration) {
    const clang::DeclContext* context = declaration.getDeclContext();
    while (context != nullptr && !(*context).isTranslationUnit()) {
        if ((*context).isStdNamespace()) return true;
        const clang::NamespaceDecl* space = llvm::dyn_cast<clang::NamespaceDecl>(context);
        if (space != nullptr && (*space).getName().starts_with("__")) return true;
        context = (*context).getParent();
    }
    return false;
}

// Whether `function` is declared in the global namespace or in `std`
// (libc++ and libstdc++ reach ::exit through `using`).
bool global_or_std(const clang::FunctionDecl& function) {
    const clang::DeclContext* space = (*function.getDeclContext()).getEnclosingNamespaceContext();
    const bool admitted = (*space).isTranslationUnit() || (*space).isStdNamespace();
    return admitted;
}

// Whether `type` (sugared) was a template parameter substituted by an
// instantiation: a dependent callable (audit E, 1.4 item 3).
bool substituted_parameter(clang::QualType type) {
    const clang::QualType bare = (*type).isReferenceType() ? (*type).getPointeeType() : type;
    const bool substituted = (*bare).getAs<clang::SubstTemplateTypeParmType>() != nullptr;
    return substituted;
}

// The declaration an expression names directly (a variable, parameter or
// member), looking through parentheses, implicit casts and one
// dereference; or null.
const clang::ValueDecl* named_declaration(const clang::Expr* expression) {
    if (expression == nullptr) return nullptr;
    const clang::Expr* bare = (*expression).IgnoreParenImpCasts();
    const clang::UnaryOperator* unary = llvm::dyn_cast<clang::UnaryOperator>(bare);
    if (unary != nullptr && (*unary).getOpcode() == clang::UO_Deref) {
        bare = (*(*unary).getSubExpr()).IgnoreParenImpCasts();
    }
    const clang::DeclRefExpr* reference = llvm::dyn_cast<clang::DeclRefExpr>(bare);
    if (reference != nullptr) return (*reference).getDecl();
    const clang::MemberExpr* member = llvm::dyn_cast<clang::MemberExpr>(bare);
    if (member != nullptr) return (*member).getMemberDecl();
    return nullptr;
}

// Whether a declaration's declared type, through its typedef chain,
// carries `mark` (an `own` alias).
bool type_alias_marked(clang::QualType type, std::string_view mark) {
    clang::QualType current = type;
    while (true) {
        const clang::TypedefType* alias = (*current).getAs<clang::TypedefType>();
        if (alias == nullptr) return false;
        const clang::TypedefNameDecl* declaration = (*alias).getDecl();
        if (has_mark(*declaration, mark)) return true;
        const clang::QualType underlying = (*declaration).getUnderlyingType();
        if (underlying == current) return false;
        current = underlying;
    }
}

// --- source positions -------------------------------------------------------------------

struct Position final {
    std::string file{};
    unsigned line{};
    unsigned column{};
};

class Sources final {
public:
    Sources(const clang::SourceManager& manager, const clang::LangOptions& language) noexcept
        : manager_(manager), language_(language) {}

    // The absolute, dot-free path with forward slashes of the file
    // `location` expands in; empty for a location without one.
    std::string file_of(clang::SourceLocation location) const {
        const clang::PresumedLoc presumed = manager_.getPresumedLoc(manager_.getExpansionLoc(location));
        if (presumed.isInvalid()) return std::string();
        llvm::SmallString<256> path(presumed.getFilename());
        // A path that cannot be made absolute stays as written; it then
        // matches no first-party root and is skipped.
        const std::error_code made = llvm::sys::fs::make_absolute(path);
        static_cast<void>(made);
        llvm::sys::path::remove_dots(path, true);
        llvm::sys::path::native(path, llvm::sys::path::Style::posix);
        std::string file = std::string(path.str());
        return file;
    }

    Position position_of(clang::SourceLocation location) const {
        const clang::PresumedLoc presumed = manager_.getPresumedLoc(manager_.getExpansionLoc(location));
        Position position{};
        if (presumed.isInvalid()) return position;
        position.file = file_of(location);
        position.line = presumed.getLine();
        position.column = presumed.getColumn();
        return position;
    }

    // The byte offset of `location`'s expansion in its file: comparable
    // between locations of one function body.
    std::uint32_t offset_of(clang::SourceLocation location) const {
        const std::uint32_t offset = manager_.getFileOffset(manager_.getExpansionLoc(location));
        return offset;
    }

    bool in_system_header(clang::SourceLocation location) const {
        const bool system = manager_.isInSystemHeader(manager_.getExpansionLoc(location));
        return system;
    }

    std::string text_of(clang::SourceRange range) const {
        const clang::CharSourceRange tokens = clang::CharSourceRange::getTokenRange(range);
        std::string text = clang::Lexer::getSourceText(tokens, manager_, language_).str();
        return text;
    }

private:
    const clang::SourceManager& manager_;
    const clang::LangOptions& language_;
};

// --- state keys ---------------------------------------------------------------------------

// What state an expression names, by spelling (`global:<name>`, `this->m`,
// `local:<id>.m`, `*<key>`), and whether that state outlives the function:
// a global or thread_local, a member, anything reached through a pointer or
// reference. Empty for an expression that names no state the rules follow.
struct StateKey final {
    std::string key{};
    bool nonlocal{};
};

std::string local_key(const clang::VarDecl& variable) {
    std::string key = "local:" + std::to_string(reinterpret_cast<std::uintptr_t>(variable.getCanonicalDecl()));
    return key;
}

StateKey state_key(const clang::Expr* expression) {
    StateKey state{};
    if (expression == nullptr) return state;
    const clang::Expr* bare = (*expression).IgnoreParenImpCasts();
    const clang::DeclRefExpr* reference = llvm::dyn_cast<clang::DeclRefExpr>(bare);
    if (reference != nullptr) {
        const clang::VarDecl* variable = llvm::dyn_cast<clang::VarDecl>((*reference).getDecl());
        if (variable == nullptr) return state;
        if ((*variable).hasGlobalStorage()) {
            state.key = "global:" + (*variable).getQualifiedNameAsString();
            state.nonlocal = true;
            return state;
        }
        state.key = local_key(*variable);
        state.nonlocal = (*(*variable).getType()).isReferenceType();
        return state;
    }
    if (llvm::isa<clang::CXXThisExpr>(bare)) {
        state.key = "this";
        state.nonlocal = true;
        return state;
    }
    const clang::MemberExpr* member = llvm::dyn_cast<clang::MemberExpr>(bare);
    if (member != nullptr) {
        const StateKey base = state_key((*member).getBase());
        if (base.key.empty()) return state;
        state.key = base.key + ((*member).isArrow() ? "->" : ".") + (*(*member).getMemberDecl()).getNameAsString();
        state.nonlocal = base.nonlocal || (*member).isArrow();
        return state;
    }
    const clang::UnaryOperator* unary = llvm::dyn_cast<clang::UnaryOperator>(bare);
    if (unary != nullptr && (*unary).getOpcode() == clang::UO_Deref) {
        const StateKey pointer = state_key((*unary).getSubExpr());
        if (pointer.key.empty()) return state;
        state.key = "*" + pointer.key;
        state.nonlocal = true;
        return state;
    }
    return state;
}

// The key of the object an address names (`&x` is x's key; a pointer `p`
// names `*p`).
StateKey pointee_key(const clang::Expr* expression) {
    StateKey state{};
    if (expression == nullptr) return state;
    const clang::Expr* bare = (*expression).IgnoreParenImpCasts();
    const clang::UnaryOperator* unary = llvm::dyn_cast<clang::UnaryOperator>(bare);
    if (unary != nullptr && (*unary).getOpcode() == clang::UO_AddrOf) return state_key((*unary).getSubExpr());
    const StateKey pointer = state_key(bare);
    if (pointer.key.empty()) return state;
    state.key = "*" + pointer.key;
    state.nonlocal = true;
    return state;
}

// The key of the owner a release names: for `delete p` and `free(p)` the
// pointer `p` itself (the owner that still holds the resource).
StateKey owner_key(const clang::Expr* expression) {
    const StateKey owner = state_key(expression);
    return owner;
}

// The owner's parent key (`this->slot.value` gives `this->slot`), for a
// released mark written on a sibling member.
std::string parent_key(const std::string& key) {
    const std::size_t dot = key.rfind('.');
    const std::size_t arrow = key.rfind("->");
    std::size_t cut = std::string::npos;
    if (dot != std::string::npos) cut = dot;
    if (arrow != std::string::npos && (cut == std::string::npos || arrow > cut)) cut = arrow;
    if (cut == std::string::npos) return std::string();
    std::string parent = key.substr(0U, cut);
    return parent;
}

// The constant spelling of a literal value (`true`, `int:0`, `nullptr`), or
// empty. A cast of a literal (an enumerator's value) is read through.
std::string literal_of(const clang::Expr* expression) {
    if (expression == nullptr) return std::string();
    const clang::Expr* bare = (*expression).IgnoreParenCasts();
    const clang::CXXBoolLiteralExpr* boolean = llvm::dyn_cast<clang::CXXBoolLiteralExpr>(bare);
    if (boolean != nullptr) return (*boolean).getValue() ? "true" : "false";
    const clang::IntegerLiteral* integer = llvm::dyn_cast<clang::IntegerLiteral>(bare);
    if (integer != nullptr) {
        const llvm::APInt& value = (*integer).getValue();
        if (value.getActiveBits() > 63U) return std::string();
        std::string spelled = "int:" + std::to_string(value.getSExtValue());
        return spelled;
    }
    if (llvm::isa<clang::CXXNullPtrLiteralExpr>(bare) || llvm::isa<clang::GNUNullExpr>(bare)) return "nullptr";
    const clang::DeclRefExpr* reference = llvm::dyn_cast<clang::DeclRefExpr>(bare);
    if (reference != nullptr && llvm::isa<clang::EnumConstantDecl>((*reference).getDecl())) {
        std::string spelled = "enum:" + (*(*reference).getDecl()).getQualifiedNameAsString();
        return spelled;
    }
    return std::string();
}

// Whether `value` is `key + 1` or `key - 1` (STXlib's counting style):
// +1, -1 or 0.
int counting_step(const clang::Expr* value, const std::string& key) {
    if (value == nullptr) return 0;
    const clang::BinaryOperator* binary = llvm::dyn_cast<clang::BinaryOperator>((*value).IgnoreParenImpCasts());
    if (binary == nullptr) return 0;
    const bool plus = (*binary).getOpcode() == clang::BO_Add;
    const bool minus = (*binary).getOpcode() == clang::BO_Sub;
    if (!plus && !minus) return 0;
    if (state_key((*binary).getLHS()).key != key || literal_of((*binary).getRHS()) != "int:1") return 0;
    const int step = plus ? 1 : -1;
    return step;
}

// --- one function body's facts -------------------------------------------------------------

enum class ExitClass : std::uint8_t { none, cancel, terminate, any };

std::string_view exit_class_name(ExitClass exit_class) noexcept {
    switch (exit_class) {
    case ExitClass::none: return "none";
    case ExitClass::cancel: return "cancel";
    case ExitClass::terminate: return "terminate";
    case ExitClass::any: return "any";
    default: return "unknown";  // every enumerator is listed; unreachable
    }
}

// How a foreign call reaches foreign code (audit E, 1.4).
enum class Reach : std::uint8_t { direct, indirect, dependent };

enum class MarkerKind : std::uint8_t { whole, guarded, no_exit };

struct SiteMarker final {
    MarkerKind kind{MarkerKind::whole};
    clang::SourceLocation location{};
    std::string reason{};
    bool has_reason{};
    // Guarded only: the guard is named, of an early_exit.guard class, and
    // live where the marker is declared.
    bool guard_named{};
    bool guard_class{};
    bool guard_live{};
    std::string guard{};
};

// What is live in scope at a point of the body.
struct LiveState final {
    bool lock{};
    bool open{};
    bool guard{};
    bool no_cancel{};
};

struct ForeignCall final {
    Reach reach{Reach::direct};
    ExitClass exit_class{ExitClass::any};
    std::uint32_t begin{};
    std::uint32_t end{};
    clang::SourceLocation location{};
    std::string callee{};
    LiveState live{};
    // The site marker covering the call's statement, an index into
    // BodyFacts::markers.
    std::optional<std::size_t> marker{};
};

// A write to state: an assignment, an increment or a decrement.
struct StateWrite final {
    std::uint32_t begin{};
    std::uint32_t end{};
    clang::SourceLocation location{};
    std::string key{};
    std::string text{};
    bool nonlocal{};
    // For a plain assignment: the assigned value's key, or its constant.
    std::string source_key{};
    std::string literal{};
};

// A copy of state into another place: `T saved = state;` or `other = state;`.
struct StateSave final {
    std::uint32_t offset{};
    std::string destination_key{};
    std::string source_key{};
};

// One half of an R5 pair (or of R2's lock/unlock).
struct PairHalf final {
    std::string family{};
    std::string key{};
    bool opens{};
    std::uint32_t offset{};
};

struct Release final {
    std::uint32_t offset{};
    clang::SourceLocation location{};
    std::string key{};
    std::string text{};
    std::string how{};
};

struct LetGo final {
    std::uint32_t offset{};
    std::string key{};
};

struct LocatedName final {
    clang::SourceLocation location{};
    std::string name{};
};

struct BodyFacts final {
    std::vector<SiteMarker> markers{};
    std::vector<ForeignCall> foreign_calls{};
    std::vector<StateWrite> writes{};
    std::vector<StateSave> saves{};
    std::vector<PairHalf> pairs{};
    std::vector<Release> releases{};
    std::vector<LetGo> let_gos{};
    std::vector<LocatedName> exit_calls{};
    std::vector<LocatedName> exit_reports{};
    std::vector<clang::SourceLocation> uncovered_catch_alls{};
    // R6's facts: each a call or expression the no-unwind path forbids.
    std::vector<LocatedName> unwinding{};
};

// The pair family and side a call's name gives (audit E, R5): `*_enter` and
// `*_leave`, `begin*`/`end*`, `acquire*`/`release*`, `push*`/`pop*`,
// `open*`/`close*`, and `lock`/`unlock` (R2's family). False when the name
// is no half.
bool pair_side(llvm::StringRef name, std::string& family, bool& opens) {
    struct Prefixes final {
        llvm::StringRef open;
        llvm::StringRef close;
    };
    if (name.ends_with("_enter") || name.ends_with("_leave")) {
        family = "enter/leave";
        opens = name.ends_with("_enter");
        return true;
    }
    if (name == "lock" || name == "unlock" || name.ends_with("_lock") || name.ends_with("_unlock")) {
        family = "lock";
        opens = name == "lock" || name.ends_with("_lock");
        return true;
    }
    const Prefixes prefixes[] = {
        {"begin", "end"}, {"acquire", "release"}, {"push", "pop"}, {"open", "close"},
    };
    for (const Prefixes& prefix : prefixes) {
        if (name.starts_with(prefix.open) || name.starts_with(prefix.close)) {
            family = prefix.open.str() + "/" + prefix.close.str();
            opens = name.starts_with(prefix.open);
            return true;
        }
    }
    return false;
}

// Walks one function body in source order and fills a BodyFacts. Lambda and
// block bodies are skipped (docs/early_exit_check.md, section 4). Its
// recursion follows statement nesting, which the parser bounds.
class BodyWalker final {
public:
    BodyWalker(const Sources& sources, const Settings& settings, bool exceptions, BodyFacts& facts) noexcept
        : sources_(sources), settings_(settings), exceptions_(exceptions), facts_(facts) {}

    void walk_body(const clang::Stmt& body) {
        scopes_.emplace_back();
        walk(&body);
        scopes_.pop_back();
    }

private:
    struct LiveObject final {
        const clang::VarDecl* variable{};
        bool guard{};
        bool lock{};
        bool open{};
        bool no_cancel{};
    };

    void walk(const clang::Stmt* statement) {
        if (statement == nullptr) return;
        if (llvm::isa<clang::LambdaExpr>(statement) || llvm::isa<clang::BlockExpr>(statement)) return;
        const clang::CompoundStmt* block = llvm::dyn_cast<clang::CompoundStmt>(statement);
        if (block != nullptr) {
            walk_block(*block);
            return;
        }
        const clang::CXXTryStmt* attempt = llvm::dyn_cast<clang::CXXTryStmt>(statement);
        if (attempt != nullptr) {
            walk_try(*attempt);
            return;
        }
        record(*statement);
        for (const clang::Stmt* child : (*statement).children()) walk(child);
    }

    // A marker covers the one statement after it; a statement with no
    // marker of its own keeps the marker covering the enclosing statement.
    void walk_block(const clang::CompoundStmt& block) {
        const std::optional<std::size_t> inherited = current_marker_;
        std::optional<std::size_t> previous{};
        scopes_.emplace_back();
        for (const clang::Stmt* child : block.body()) {
            current_marker_ = previous.has_value() ? previous : inherited;
            const std::size_t markers_before = facts_.markers.size();
            walk(child);
            previous.reset();
            if (llvm::isa<clang::DeclStmt>(child) && facts_.markers.size() > markers_before) {
                previous = facts_.markers.size() - 1U;
            }
        }
        scopes_.pop_back();
        current_marker_ = inherited;
    }

    void walk_try(const clang::CXXTryStmt& attempt) {
        walk(attempt.getTryBlock());
        for (unsigned index = 0U; index < attempt.getNumHandlers(); ++index) {
            const clang::CXXCatchStmt* handler = attempt.getHandler(index);
            if (exceptions_ && (*handler).getExceptionDecl() == nullptr && !handler_covers_forced_unwind(*handler)) {
                facts_.uncovered_catch_alls.push_back((*handler).getCatchLoc());
            }
            walk((*handler).getHandlerBlock());
        }
    }

    // R10: the handler holds `throw;`, or its first call in source order is
    // to an early_exit.forced_unwind function. Pre-order with an explicit
    // work list, children pushed in reverse to keep source order.
    static bool handler_covers_forced_unwind(const clang::CXXCatchStmt& handler) {
        bool first_call_seen = false;
        bool first_call_covers = false;
        std::vector<const clang::Stmt*> pending{};
        pending.push_back(handler.getHandlerBlock());
        while (!pending.empty()) {
            const clang::Stmt* current = pending.back();
            pending.pop_back();
            if (current == nullptr || llvm::isa<clang::LambdaExpr>(current)) continue;
            const clang::CXXThrowExpr* thrown = llvm::dyn_cast<clang::CXXThrowExpr>(current);
            if (thrown != nullptr && (*thrown).getSubExpr() == nullptr) return true;
            const clang::CallExpr* call = llvm::dyn_cast<clang::CallExpr>(current);
            if (call != nullptr && !first_call_seen) {
                first_call_seen = true;
                const clang::FunctionDecl* callee = (*call).getDirectCallee();
                first_call_covers = callee != nullptr && function_marked(*callee, mark_forced_unwind);
            }
            std::vector<const clang::Stmt*> children{};
            for (const clang::Stmt* child : (*current).children()) children.push_back(child);
            for (std::size_t index = children.size(); index > 0U; --index) pending.push_back(children[index - 1U]);
        }
        return first_call_covers;
    }

    LiveState live_state() const {
        LiveState state{};
        for (const std::vector<LiveObject>& scope : scopes_) {
            for (const LiveObject& object : scope) {
                state.lock = state.lock || object.lock;
                state.open = state.open || object.open;
                state.guard = state.guard || object.guard;
                state.no_cancel = state.no_cancel || object.no_cancel;
            }
        }
        return state;
    }

    const LiveObject* live_object(const clang::VarDecl& variable) const {
        for (const std::vector<LiveObject>& scope : scopes_) {
            for (const LiveObject& object : scope) {
                if ((*object.variable).getCanonicalDecl() == variable.getCanonicalDecl()) return &object;
            }
        }
        return nullptr;
    }

    void record(const clang::Stmt& statement) {
        if (llvm::isa<clang::CXXThrowExpr>(statement)) {
            LocatedName thrown{};
            thrown.location = statement.getBeginLoc();
            thrown.name = "a throw";
            facts_.unwinding.push_back(thrown);
            return;
        }
        const clang::DeclStmt* declaration = llvm::dyn_cast<clang::DeclStmt>(&statement);
        if (declaration != nullptr) {
            for (const clang::Decl* declared : (*declaration).decls()) {
                const clang::VarDecl* variable = llvm::dyn_cast<clang::VarDecl>(declared);
                if (variable != nullptr) record_variable(*variable);
            }
            return;
        }
        const clang::BinaryOperator* binary = llvm::dyn_cast<clang::BinaryOperator>(&statement);
        if (binary != nullptr && (*binary).isAssignmentOp()) {
            record_assignment(statement, (*binary).getOpcode(), (*binary).getLHS(), (*binary).getRHS());
            return;
        }
        const clang::UnaryOperator* unary = llvm::dyn_cast<clang::UnaryOperator>(&statement);
        if (unary != nullptr && (*unary).isIncrementDecrementOp()) {
            record_write(statement, (*unary).getSubExpr(), nullptr);
            const StateKey counted = state_key((*unary).getSubExpr());
            if (!counted.key.empty()) add_half("count", counted.key, (*unary).isIncrementOp(), statement.getBeginLoc());
            return;
        }
        // A class type's assignment operator is also a call: its write is
        // recorded here and the call below.
        const clang::CXXOperatorCallExpr* operator_call = llvm::dyn_cast<clang::CXXOperatorCallExpr>(&statement);
        if (operator_call != nullptr && (*operator_call).isAssignmentOp() && (*operator_call).getNumArgs() == 2U) {
            const clang::OverloadedOperatorKind kind = (*operator_call).getOperator();
            clang::BinaryOperatorKind as_binary = clang::BO_Assign;
            if (kind == clang::OO_PlusEqual) as_binary = clang::BO_AddAssign;
            if (kind == clang::OO_MinusEqual) as_binary = clang::BO_SubAssign;
            const bool modelled = kind == clang::OO_Equal || kind == clang::OO_PlusEqual || kind == clang::OO_MinusEqual;
            if (modelled) record_assignment(statement, as_binary, (*operator_call).getArg(0U), (*operator_call).getArg(1U));
        }
        const clang::CXXNewExpr* allocation = llvm::dyn_cast<clang::CXXNewExpr>(&statement);
        if (allocation != nullptr) {
            const clang::FunctionDecl* operator_new = (*allocation).getOperatorNew();
            if (operator_new != nullptr && !function_nothrow(*operator_new)) {
                LocatedName throwing{};
                throwing.location = statement.getBeginLoc();
                throwing.name = "a throwing operator new";
                facts_.unwinding.push_back(throwing);
            }
            return;
        }
        const clang::CXXDeleteExpr* deletion = llvm::dyn_cast<clang::CXXDeleteExpr>(&statement);
        if (deletion != nullptr) {
            add_release(owner_key((*deletion).getArgument()), statement, "delete");
            return;
        }
        const clang::ObjCMessageExpr* message = llvm::dyn_cast<clang::ObjCMessageExpr>(&statement);
        if (message != nullptr) {
            record_message(*message);
            return;
        }
        const clang::CallExpr* call = llvm::dyn_cast<clang::CallExpr>(&statement);
        if (call != nullptr) record_call(*call);
    }

    // A local: a scope object (guard, lock, open, no_cancel), a site
    // marker, or a save of state into a local.
    void record_variable(const clang::VarDecl& variable) {
        const clang::CXXRecordDecl* record = record_of(variable.getType());
        if (record != nullptr) {
            if (marker_type(*record, marker_whole) || marker_type(*record, marker_guarded) ||
                marker_type(*record, marker_no_exit)) {
                record_marker(variable, *record);
                return;
            }
            LiveObject object{};
            object.variable = &variable;
            object.guard = record_marked(*record, mark_guard);
            const std::string name = record_name(*record);
            object.lock = record_marked(*record, mark_lock) || name == "std::lock_guard" ||
                          name == "std::unique_lock" || name == "std::scoped_lock" || name == "std::shared_lock";
            object.open = record_marked(*record, mark_open);
            object.no_cancel = record_marked(*record, mark_no_cancel);
            if (!scopes_.empty()) scopes_.back().push_back(object);
        }
        const clang::Expr* initializer = variable.getInit();
        const StateKey source = state_key(initializer);
        if (!source.key.empty()) {
            StateSave save{};
            save.offset = sources_.offset_of(variable.getLocation());
            save.destination_key = local_key(variable);
            save.source_key = source.key;
            facts_.saves.push_back(save);
        }
    }

    // The first string literal and the first variable an initializer names,
    // in a pre-order walk.
    static void marker_arguments(const clang::Expr* initializer, const clang::StringLiteral*& reason,
                                 const clang::VarDecl*& guard) {
        std::vector<const clang::Stmt*> pending{};
        pending.push_back(initializer);
        while (!pending.empty()) {
            const clang::Stmt* current = pending.back();
            pending.pop_back();
            if (current == nullptr) continue;
            const clang::StringLiteral* literal = llvm::dyn_cast<clang::StringLiteral>(current);
            if (literal != nullptr && reason == nullptr) reason = literal;
            const clang::DeclRefExpr* reference = llvm::dyn_cast<clang::DeclRefExpr>(current);
            if (reference != nullptr && guard == nullptr) {
                guard = llvm::dyn_cast<clang::VarDecl>((*reference).getDecl());
            }
            std::vector<const clang::Stmt*> children{};
            for (const clang::Stmt* child : (*current).children()) children.push_back(child);
            for (std::size_t index = children.size(); index > 0U; --index) pending.push_back(children[index - 1U]);
        }
    }

    void record_marker(const clang::VarDecl& variable, const clang::CXXRecordDecl& record) {
        SiteMarker marker{};
        marker.location = variable.getLocation();
        if (marker_type(record, marker_guarded)) {
            marker.kind = MarkerKind::guarded;
        } else if (marker_type(record, marker_no_exit)) {
            marker.kind = MarkerKind::no_exit;
        } else {
            marker.kind = MarkerKind::whole;
        }
        const clang::StringLiteral* reason = nullptr;
        const clang::VarDecl* guard = nullptr;
        marker_arguments(variable.getInit(), reason, guard);
        if (reason != nullptr && (*reason).isOrdinary()) {
            marker.reason = (*reason).getString().str();
            marker.has_reason = !marker.reason.empty();
        }
        if (marker.kind == MarkerKind::guarded && guard != nullptr) {
            marker.guard_named = true;
            marker.guard = (*guard).getNameAsString();
            const clang::CXXRecordDecl* guard_record = record_of((*guard).getType());
            marker.guard_class = guard_record != nullptr && record_marked(*guard_record, mark_guard);
            marker.guard_live = live_object(*guard) != nullptr;
        }
        facts_.markers.push_back(std::move(marker));
    }

    void record_assignment(const clang::Stmt& statement, clang::BinaryOperatorKind kind, const clang::Expr* target,
                           const clang::Expr* value) {
        const bool plain = kind == clang::BO_Assign;
        record_write(statement, target, plain ? value : nullptr);
        const StateKey written = state_key(target);
        if (written.key.empty()) return;
        int step = 0;
        if (plain) step = counting_step(value, written.key);
        if ((kind == clang::BO_AddAssign || kind == clang::BO_SubAssign) && literal_of(value) == "int:1") {
            step = kind == clang::BO_AddAssign ? 1 : -1;
        }
        if (step != 0) add_half("count", written.key, step > 0, statement.getBeginLoc());
    }

    void record_write(const clang::Stmt& statement, const clang::Expr* target, const clang::Expr* value) {
        const StateKey written = state_key(target);
        if (written.key.empty()) return;
        StateWrite write{};
        write.begin = sources_.offset_of(statement.getBeginLoc());
        write.end = sources_.offset_of(statement.getEndLoc());
        write.location = statement.getBeginLoc();
        write.key = written.key;
        write.text = sources_.text_of((*target).getSourceRange());
        write.nonlocal = written.nonlocal;
        if (value != nullptr) {
            const StateKey source = state_key(value);
            write.source_key = source.key;
            write.literal = literal_of(value);
            if (!source.key.empty()) {
                StateSave save{};
                save.offset = write.begin;
                save.destination_key = written.key;
                save.source_key = source.key;
                facts_.saves.push_back(save);
            }
            if (!write.literal.empty()) {
                LetGo let_go{};
                let_go.offset = write.begin;
                let_go.key = written.key;
                facts_.let_gos.push_back(let_go);
            }
        }
        facts_.writes.push_back(std::move(write));
    }

    void add_half(std::string family, std::string key, bool opens, clang::SourceLocation location) {
        PairHalf half{};
        half.family = std::move(family);
        half.key = std::move(key);
        half.opens = opens;
        half.offset = sources_.offset_of(location);
        facts_.pairs.push_back(std::move(half));
    }

    void add_release(const StateKey& owner, const clang::Stmt& statement, std::string how) {
        if (owner.key.empty() || !owner.nonlocal) return;
        Release release{};
        release.offset = sources_.offset_of(statement.getBeginLoc());
        release.location = statement.getBeginLoc();
        release.key = owner.key;
        release.text = sources_.text_of(statement.getSourceRange());
        release.how = std::move(how);
        facts_.releases.push_back(std::move(release));
    }

    void add_foreign(Reach reach, ExitClass exit_class, const clang::Expr& call, std::string callee) {
        const LiveState live = live_state();
        ForeignCall foreign{};
        foreign.reach = reach;
        foreign.exit_class = exit_class == ExitClass::cancel && live.no_cancel ? ExitClass::none : exit_class;
        foreign.begin = sources_.offset_of(call.getBeginLoc());
        foreign.end = sources_.offset_of(call.getEndLoc());
        foreign.location = call.getBeginLoc();
        foreign.callee = std::move(callee);
        foreign.live = live;
        foreign.marker = current_marker_;
        facts_.foreign_calls.push_back(std::move(foreign));
    }

    void record_message(const clang::ObjCMessageExpr& message) {
        const clang::ObjCMethodDecl* method = message.getMethodDecl();
        if (method == nullptr || !sources_.in_system_header((*method).getLocation())) return;
        add_foreign(Reach::direct, ExitClass::any, message, (*method).getSelector().getAsString());
    }

    // The exit class of a direct foreign callee (audit E, 1.4). `derived`
    // admits the derived no-exit table (a noexcept callee taking no
    // callable), which applies to unmarked host functions only: a function
    // marked foreign is class any unless the tables or its marks say
    // otherwise.
    ExitClass direct_exit_class(const clang::FunctionDecl& function, const std::string& name, bool derived) const {
        if (function_marked(function, mark_no_exit) || matches_any(settings_.no_exit_functions, name)) {
            return ExitClass::none;
        }
        if (function_marked(function, mark_cancel) || matches_any(settings_.cancellation_points, name)) {
            return ExitClass::cancel;
        }
        if (derived && function_nothrow(function) && !takes_callable(function)) return ExitClass::none;
        return ExitClass::any;
    }

    // Whether `function` is our own code for R6: neither in a system header
    // (the standard library counts as ours when the project says so) nor
    // under a foreign path.
    bool own_function(const clang::FunctionDecl& function) const {
        const std::string declared_in = sources_.file_of(function.getLocation());
        if (!declared_in.empty() && under_any(declared_in, settings_.foreign_paths)) return false;
        if (!sources_.in_system_header(function.getLocation())) return true;
        const bool own = in_standard_library(function) && !settings_.standard_library_foreign;
        return own;
    }

    void record_call(const clang::CallExpr& call) {
        if (call.isTypeDependent() || call.isValueDependent()) return;
        const clang::FunctionDecl* function = call.getDirectCallee();
        if (function != nullptr) record_direct_facts(call, *function);
        if (record_dependent_call(call, function)) return;
        if (function == nullptr) {
            record_indirect_call(call);
            return;
        }
        if ((*function).getBuiltinID() != 0U) return;
        const std::string name = qualified_name(*function);
        const clang::CXXMethodDecl* method = llvm::dyn_cast<clang::CXXMethodDecl>(function);
        const bool marked = method != nullptr ? method_marked(*method, mark_foreign)
                                              : function_marked(*function, mark_foreign);
        if (marked || matches_any(settings_.foreign_functions, name)) {
            add_foreign(Reach::direct, direct_exit_class(*function, name, false), call, name);
            return;
        }
        if (method != nullptr && virtual_dispatch(call, *method) && !own_virtual(*method)) {
            const ExitClass exit_class = function_nothrow(*method) ? ExitClass::terminate : ExitClass::any;
            add_foreign(Reach::indirect, exit_class, call, name);
            add_unwinding(call, "the virtual call " + name);
            return;
        }
        if (own_function(*function)) return;
        add_foreign(Reach::direct, direct_exit_class(*function, name, true), call, name);
    }

    // A dependent callable: the callee object, or the variable called, was
    // declared with a template parameter's type. True when recorded.
    bool record_dependent_call(const clang::CallExpr& call, const clang::FunctionDecl* function) {
        const clang::Expr* object = nullptr;
        const clang::CXXOperatorCallExpr* operator_call = llvm::dyn_cast<clang::CXXOperatorCallExpr>(&call);
        if (operator_call != nullptr && (*operator_call).getOperator() == clang::OO_Call &&
            (*operator_call).getNumArgs() > 0U) {
            object = (*operator_call).getArg(0U);
        }
        const clang::CXXMemberCallExpr* member_call = llvm::dyn_cast<clang::CXXMemberCallExpr>(&call);
        if (member_call != nullptr) object = (*member_call).getImplicitObjectArgument();
        if (object == nullptr && function == nullptr) object = call.getCallee();
        const clang::ValueDecl* named = named_declaration(object);
        if (named == nullptr || !substituted_parameter((*named).getType())) return false;
        const bool nothrow = function != nullptr ? function_nothrow(*function)
                                                 : callee_type_nothrow((*call.getCallee()).getType());
        const std::string callee = sources_.text_of(call.getSourceRange());
        add_foreign(Reach::dependent, nothrow ? ExitClass::terminate : ExitClass::any, call, callee);
        add_unwinding(call, "the dependent call " + callee);
        return true;
    }

    void record_indirect_call(const clang::CallExpr& call) {
        const clang::Expr* target = call.getCallee();
        if (target == nullptr) return;
        const clang::Expr* bare = (*target).IgnoreParenImpCasts();
        if (llvm::isa<clang::CXXPseudoDestructorExpr>(bare)) return;
        clang::QualType type = (*target).getType();
        const clang::BinaryOperator* binding = llvm::dyn_cast<clang::BinaryOperator>(bare);
        if (binding != nullptr && (*binding).isPtrMemOp()) type = (*(*binding).getRHS()).getType();
        const bool nothrow = callee_type_nothrow(type);
        const clang::ValueDecl* named = named_declaration(binding != nullptr ? (*binding).getRHS() : target);
        const bool own = named != nullptr &&
                         (has_mark(*named, mark_own) || type_alias_marked((*named).getType(), mark_own));
        if (!(own && nothrow)) add_unwinding(call, "an indirect call not through a noexcept own alias");
        if (own) return;
        const std::string callee = sources_.text_of((*target).getSourceRange());
        add_foreign(Reach::indirect, nothrow ? ExitClass::terminate : ExitClass::any, call, callee);
    }

    // Whether a virtual method's dispatch stays in our code by an `own`
    // mark on the method (or one it overrides) or on its class (an
    // extension of STXlib's vocabulary, docs/early_exit_check.md).
    static bool own_virtual(const clang::CXXMethodDecl& method) {
        const bool own = method_marked(method, mark_own) || record_marked(*method.getParent(), mark_own);
        return own;
    }

    // Whether a call of `method` dispatches virtually: the method is
    // virtual and not final, its class is not final, and the call names no
    // qualifier.
    static bool virtual_dispatch(const clang::CallExpr& call, const clang::CXXMethodDecl& method) {
        if (!method.isVirtual() || method.hasAttr<clang::FinalAttr>()) return false;
        if ((*method.getParent()).hasAttr<clang::FinalAttr>()) return false;
        const clang::CXXMemberCallExpr* member_call = llvm::dyn_cast<clang::CXXMemberCallExpr>(&call);
        if (member_call == nullptr) return false;
        const clang::MemberExpr* member =
            llvm::dyn_cast<clang::MemberExpr>((*(*member_call).getCallee()).IgnoreParenImpCasts());
        if (member != nullptr && (*member).hasQualifier()) return false;
        return true;
    }

    void add_unwinding(const clang::Expr& call, std::string name) {
        LocatedName unwinding{};
        unwinding.location = call.getBeginLoc();
        unwinding.name = std::move(name);
        facts_.unwinding.push_back(std::move(unwinding));
    }

    // The facts a direct call gives whether or not it is foreign: exit
    // calls (R8), releases and let-gos (R9), pair halves (R2, R5) and the
    // no-unwind facts (R6).
    void record_direct_facts(const clang::CallExpr& call, const clang::FunctionDecl& function) {
        const std::string name = qualified_name(function);
        const clang::IdentifierInfo* identifier = function.getIdentifier();
        const llvm::StringRef plain = identifier != nullptr ? (*identifier).getName() : llvm::StringRef();
        if (identifier != nullptr && global_or_std(function)) {
            LocatedName exit_call{};
            exit_call.location = call.getBeginLoc();
            exit_call.name = plain.str();
            if (plain == "exit" || plain == "quick_exit" || plain == "at_quick_exit") {
                facts_.exit_calls.push_back(exit_call);
            } else if (plain == "_Exit" || plain == "_exit" || plain == "ExitProcess" || plain == "TerminateProcess") {
                facts_.exit_reports.push_back(exit_call);
            }
        }
        record_release_facts(call, function, name);
        record_pair_half(call, function, plain);
        record_no_unwind_facts(call, function, name, plain);
    }

    void record_release_facts(const clang::CallExpr& call, const clang::FunctionDecl& function,
                              const std::string& name) {
        const clang::CXXMemberCallExpr* member_call = llvm::dyn_cast<clang::CXXMemberCallExpr>(&call);
        if (member_call != nullptr) {
            const clang::Expr* object = (*member_call).getImplicitObjectArgument();
            if (llvm::isa<clang::CXXDestructorDecl>(&function)) {
                add_release(state_key(object), call, "an explicit destructor call");
                return;
            }
            const clang::CXXRecordDecl* owner = (*member_call).getRecordDecl();
            const std::string owner_name = owner != nullptr ? record_name(*owner) : std::string();
            const bool replaces_first = owner_name == "std::unique_ptr" || owner_name == "std::shared_ptr";
            const clang::IdentifierInfo* identifier = function.getIdentifier();
            const bool reset = identifier != nullptr && (*identifier).getName() == "reset";
            if (reset && call.getNumArgs() == 0U && !replaces_first) {
                add_release(state_key(object), call, "reset()");
            }
            return;
        }
        if (call.getNumArgs() == 0U) return;
        if (name == "std::destroy_at") {
            add_release(pointee_key(call.getArg(0U)), call, "std::destroy_at");
            return;
        }
        if (name == "std::exchange" || name == "std::move") {
            const StateKey owner = state_key(call.getArg(0U));
            if (!owner.key.empty()) {
                LetGo let_go{};
                let_go.offset = sources_.offset_of(call.getBeginLoc());
                let_go.key = owner.key;
                facts_.let_gos.push_back(let_go);
            }
            return;
        }
        if (matches_any(settings_.release_functions, name)) add_release(owner_key(call.getArg(0U)), call, name);
    }

    void record_pair_half(const clang::CallExpr& call, const clang::FunctionDecl& function, llvm::StringRef plain) {
        if (plain.empty()) return;
        std::string family{};
        bool opens = false;
        if (!pair_side(plain, family, opens)) return;
        // The standard library's begin/end and push/pop are no pairs; its
        // lock/unlock (std::mutex) are R2's.
        if (family != "lock" && in_standard_library(function)) return;
        const clang::CXXMemberCallExpr* member_call = llvm::dyn_cast<clang::CXXMemberCallExpr>(&call);
        StateKey key{};
        if (member_call != nullptr) {
            const clang::CXXMethodDecl* method = (*member_call).getMethodDecl();
            if (method != nullptr && (*method).isConst()) return;
            key = state_key((*member_call).getImplicitObjectArgument());
        } else if (call.getNumArgs() > 0U) {
            key = pointee_key(call.getArg(0U));
            if (key.key.empty()) key = state_key(call.getArg(0U));
        }
        if (key.key.empty()) return;
        add_half(std::move(family), key.key, opens, call.getBeginLoc());
    }

    void record_no_unwind_facts(const clang::CallExpr& call, const clang::FunctionDecl& function,
                                const std::string& name, llvm::StringRef plain) {
        if (plain == "setjmp" || plain == "_setjmp" || plain == "sigsetjmp" || plain == "longjmp" ||
            plain == "_longjmp" || plain == "siglongjmp") {
            add_unwinding(call, plain.str());
            return;
        }
        if (name == "std::get_new_handler" || name == "std::set_new_handler") {
            add_unwinding(call, name);
            return;
        }
        if (function.getBuiltinID() != 0U) return;
        if (own_function(function) && !function_marked(function, mark_foreign) &&
            !matches_any(settings_.foreign_functions, name)) {
            if (!function_marked(function, mark_no_unwind) && !function_nothrow(function) && !function.isExternC()) {
                add_unwinding(call, name + ", neither no_unwind nor noexcept");
            }
            return;
        }
        if (!matches_any(settings_.allocator_allowlist, name)) {
            add_unwinding(call, name + ", a foreign call outside the allocator allowlist");
        }
    }

    const Sources& sources_;
    const Settings& settings_;
    const bool exceptions_;
    BodyFacts& facts_;
    std::vector<std::vector<LiveObject>> scopes_{};
    std::optional<std::size_t> current_marker_{};
};

// --- the rules over one function ---------------------------------------------------------

class FunctionChecker final {
public:
    FunctionChecker(const Sources& sources, const Settings& settings, bool exceptions, EntrySet& entries) noexcept
        : sources_(sources), settings_(settings), exceptions_(exceptions), entries_(entries) {}

    void check_definition(const clang::FunctionDecl& function) {
        const clang::Stmt* body = function.getBody();
        if (body == nullptr) return;
        BodyFacts facts{};
        BodyWalker walker(sources_, settings_, exceptions_, facts);
        walker.walk_body(*body);
        const std::string name = qualified_name(function);
        const bool whole_body = function_marked(function, mark_whole);
        check_markers(facts, name);
        for (const ForeignCall& call : facts.foreign_calls) check_call(facts, call, whole_body, name);
        check_releases(facts, name);
        for (const LocatedName& call : facts.exit_calls) {
            add("R8", Level::finding, "P10", call.location, name,
                call.name + ": our code calls no exit or quick_exit; fail fast with abort");
        }
        for (const LocatedName& call : facts.exit_reports) {
            add("R8", Level::report, "P10", call.location, name,
                call.name + " ends the process without abort (not named by the decision)");
        }
        for (const clang::SourceLocation location : facts.uncovered_catch_alls) {
            add("R10", Level::finding, "P6", location, name,
                "catch (...) neither rethrows nor first calls an early_exit.forced_unwind function, so it can "
                "swallow a forced unwind");
        }
        if (function_marked(function, mark_no_unwind)) {
            for (const LocatedName& unwinding : facts.unwinding) {
                add("R6", Level::finding, "P8", unwinding.location, name,
                    "the no-unwind path reaches " + unwinding.name);
            }
        }
    }

    // R6's declaration half.
    void check_no_unwind_declaration(const clang::FunctionDecl& function) {
        if (!function_marked(function, mark_no_unwind) || function_nothrow(function)) return;
        add("R6", Level::finding, "P8", function.getLocation(), qualified_name(function),
            "an early_exit.no_unwind function must be declared noexcept");
    }

    // R7.
    void check_thread_local(const clang::VarDecl& variable) {
        if (variable.getTLSKind() == clang::VarDecl::TLS_None) return;
        if ((*variable.getType()).isDependentType()) return;
        const std::string name = variable.getQualifiedNameAsString();
        if (variable.getType().isDestructedType() != clang::QualType::DK_none) {
            add("R7", Level::finding, "P9", variable.getLocation(), name,
                "a thread_local must be trivially destructible: keep a pointer, index or flag here and the "
                "state in owned per-thread records");
            return;
        }
        if (!variable.hasAttr<clang::ConstInitAttr>() && variable.getTLSKind() == clang::VarDecl::TLS_Dynamic) {
            add("R7", Level::report, "P9", variable.getLocation(), name,
                "a thread_local with dynamic initialization (not constinit)");
        }
    }

private:
    const SiteMarker* marker_of(const BodyFacts& facts, const ForeignCall& call) const {
        if (!call.marker.has_value()) return nullptr;
        const SiteMarker* marker = &facts.markers[*call.marker];
        return marker;
    }

    // R1 (reasons), R3 and the NoExit listing.
    void check_markers(const BodyFacts& facts, const std::string& name) {
        for (const SiteMarker& marker : facts.markers) {
            if (!marker.has_reason) {
                add("R1", Level::finding, "", marker.location, name,
                    "a site marker's reason is mandatory: write which invariants hold, what the guard restores, "
                    "or why the callee cannot exit");
            }
            if (marker.kind == MarkerKind::no_exit) {
                const bool accepted = llvm::StringRef(marker.reason).starts_with(
                    llvm::StringRef(accepted_terminate.data(), accepted_terminate.size()));
                add("R1", Level::report, accepted ? "P13" : "", marker.location, name, "NoExit: " + marker.reason);
            }
            if (marker.kind != MarkerKind::guarded) continue;
            if (!exceptions_) {
                add("R3", Level::finding, "", marker.location, name,
                    "a Guarded marker in a unit compiled without exceptions: no guard runs on an early exit "
                    "here, so only form 1 is available");
            } else if (!marker.guard_named) {
                add("R3", Level::finding, "", marker.location, name, "a Guarded marker that names no guard object");
            } else if (!marker.guard_class) {
                add("R3", Level::finding, "", marker.location, name,
                    "guard `" + marker.guard + "` is not of an early_exit.guard class");
            } else if (!marker.guard_live) {
                add("R3", Level::finding, "", marker.location, name,
                    "guard `" + marker.guard + "` is not live here (declare it earlier in an enclosing block)");
            }
        }
    }

    bool valid_guarded(const SiteMarker* marker) const {
        const bool valid = marker != nullptr && (*marker).kind == MarkerKind::guarded && exceptions_ &&
                           (*marker).guard_named && (*marker).guard_class && (*marker).guard_live;
        return valid;
    }

    static std::string_view pattern_for_indirect(const ForeignCall& call, std::string_view pattern) {
        if (call.reach == Reach::direct) return "";
        return pattern;
    }

    // R1, R2, R4 and R5 at one foreign call.
    void check_call(const BodyFacts& facts, const ForeignCall& call, bool whole_body, const std::string& name) {
        if (call.exit_class == ExitClass::none) return;
        const SiteMarker* marker = marker_of(facts, call);
        const std::string described =
            call.callee + ", exit class " + std::string(exit_class_name(call.exit_class));
        if (marker == nullptr && !whole_body) {
            const std::string_view pattern =
                call.reach != Reach::direct && call.exit_class == ExitClass::any ? "P7" : "";
            add("R1", Level::finding, pattern, call.location, name,
                "unannotated foreign call (" + described +
                    "): mark it Whole, Guarded or NoExit, or mark the function early_exit.whole");
        }
        const bool in_lock_pair = between_pair(facts, call, "lock");
        if (call.live.lock || in_lock_pair) {
            std::string_view pattern = pattern_for_indirect(call, "P2");
            if (call.exit_class == ExitClass::cancel) pattern = "P1";
            if (llvm::StringRef(call.callee).contains("cond_") || llvm::StringRef(call.callee).contains("join")) {
                pattern = "P3";
            }
            add("R2", Level::finding, pattern, call.location, name,
                "foreign call (" + described +
                    ") while a lock is held: a callee that never returns holds it forever; move the call "
                    "outside the lock, or (class cancel) disable cancellation around it");
        }
        const bool whole_claimed = whole_body || (marker != nullptr && (*marker).kind == MarkerKind::whole);
        if (whole_claimed && (call.live.open || call.live.lock)) {
            add("R4", Level::finding, "", call.location, name,
                "a Whole claim while an open or lock object is live: the invariants are not whole here");
        }
        const bool cancel_or_any = call.exit_class == ExitClass::cancel || call.exit_class == ExitClass::any;
        if (!cancel_or_any) return;
        if (call.live.open && !valid_guarded(marker)) {
            add("R4", Level::finding, pattern_for_indirect(call, "P4"), call.location, name,
                "foreign call (" + described + ") while an open invariant is live, with no valid Guarded marker");
        }
        const bool guard_covers = exceptions_ && call.live.guard;
        if (guard_covers) return;
        const std::string pair = open_pair(facts, call);
        if (!pair.empty()) {
            add("R5", Level::finding, pattern_for_indirect(call, "P4"), call.location, name,
                "foreign call (" + described + ") between the halves of " + pair +
                    " with no live guard: an early exit skips the second half");
        }
    }

    // Whether `call` lies between an opening half of `family` and a later
    // closing half on the same key.
    static bool between_pair(const BodyFacts& facts, const ForeignCall& call, std::string_view family) {
        for (const PairHalf& opening : facts.pairs) {
            if (!opening.opens || opening.family != family || opening.offset >= call.begin) continue;
            for (const PairHalf& closing : facts.pairs) {
                if (!closing.opens && closing.family == family && closing.key == opening.key &&
                    closing.offset > call.end) {
                    return true;
                }
            }
        }
        return false;
    }

    // The R5 pair `call` lies inside, described for the message, or empty:
    // a counting, call or flag pair, or a save and restore by hand.
    std::string open_pair(const BodyFacts& facts, const ForeignCall& call) const {
        for (const PairHalf& opening : facts.pairs) {
            if (!opening.opens || opening.family == "lock" || opening.offset >= call.begin) continue;
            for (const PairHalf& closing : facts.pairs) {
                if (!closing.opens && closing.family == opening.family && closing.key == opening.key &&
                    closing.offset > call.end) {
                    std::string described = "a " + opening.family + " pair";
                    return described;
                }
            }
        }
        for (const StateWrite& later : facts.writes) {
            if (!later.nonlocal || later.begin <= call.end) continue;
            for (const StateWrite& earlier : facts.writes) {
                if (earlier.key != later.key || earlier.end >= call.begin) continue;
                const bool flags = (earlier.literal == "true" || earlier.literal == "false") &&
                                   (later.literal == "true" || later.literal == "false") &&
                                   earlier.literal != later.literal;
                if (flags) {
                    std::string described = "the flag `" + later.text + "` set and reset";
                    return described;
                }
                if (restores(facts, earlier, later)) {
                    const Position changed = sources_.position_of(earlier.location);
                    const Position restored = sources_.position_of(later.location);
                    std::string described = "`" + later.text + "` changed at line " + std::to_string(changed.line) +
                                            " and restored by hand at line " + std::to_string(restored.line);
                    return described;
                }
            }
        }
        return std::string();
    }

    // Whether `later` assigns back a copy of the state saved before
    // `earlier` changed it (GUI.Forms' top_frame pattern).
    static bool restores(const BodyFacts& facts, const StateWrite& earlier, const StateWrite& later) {
        if (later.source_key.empty()) return false;
        for (const StateSave& save : facts.saves) {
            if (save.offset <= earlier.begin && save.source_key == later.key &&
                save.destination_key == later.source_key) {
                return true;
            }
        }
        return false;
    }

    // R9: a release while the owner still holds the resource.
    void check_releases(const BodyFacts& facts, const std::string& name) {
        for (const Release& release : facts.releases) {
            if (let_go_before(facts, release)) continue;
            add("R9", Level::finding, "P12", release.location, name,
                release.how + " of `" + release.text +
                    "` while its owner still holds it: clear the owner (or mark the slot released, or move it into a "
                    "local) first, so nothing is ever freed twice");
        }
    }

    // Whether the owner was let go of before `release`: the owner itself,
    // or (a released mark on a sibling) another member of the same slot.
    // A slot is a member of something (`this->slot` in `this->slot.value`),
    // never a root such as `this`, so one member's reset does not count for
    // another member of the object.
    static bool let_go_before(const BodyFacts& facts, const Release& release) {
        std::string slot = parent_key(release.key);
        if (parent_key(slot).empty()) slot.clear();
        for (const LetGo& let_go : facts.let_gos) {
            if (let_go.offset >= release.offset) continue;
            if (let_go.key == release.key) return true;
            if (!slot.empty() && parent_key(let_go.key) == slot) return true;
        }
        return false;
    }

    void add(std::string_view rule, Level level, std::string_view pattern, clang::SourceLocation location,
             const std::string& function, std::string message) {
        const Position position = sources_.position_of(location);
        if (position.file.empty() || !under_any(position.file, settings_.first_party_roots)) return;
        Entry entry{};
        entry.rule = std::string(rule);
        entry.level = level;
        entry.pattern = std::string(pattern);
        entry.file = position.file;
        entry.line = position.line;
        entry.column = position.column;
        entry.function = function;
        entry.message = std::move(message);
        entries_.add(std::move(entry));
    }

    const Sources& sources_;
    const Settings& settings_;
    const bool exceptions_;
    EntrySet& entries_;
};

// --- the AST walk ---------------------------------------------------------------------------

class Visitor final : public clang::RecursiveASTVisitor<Visitor> {
public:
    Visitor(const clang::ASTContext& context, const Settings& settings, EntrySet& entries) noexcept
        : settings_(settings), sources_(context.getSourceManager(), context.getLangOpts()),
          checker_(sources_, settings, context.getLangOpts().CXXExceptions, entries) {}

    // Templates are read through their instantiations, whose calls are
    // resolved; dependent patterns are skipped.
    bool shouldVisitTemplateInstantiations() const { return true; }

    bool VisitFunctionDecl(clang::FunctionDecl* function) {
        if (function == nullptr || (*function).isImplicit() || (*function).isInvalidDecl()) return true;
        if (!first_party((*function).getLocation())) return true;
        checker_.check_no_unwind_declaration(*function);
        if ((*function).isDependentContext()) return true;
        if ((*function).doesThisDeclarationHaveABody()) checker_.check_definition(*function);
        return true;
    }

    bool VisitVarDecl(clang::VarDecl* variable) {
        if (variable == nullptr || (*variable).isInvalidDecl()) return true;
        if (!first_party((*variable).getLocation())) return true;
        checker_.check_thread_local(*variable);
        return true;
    }

private:
    bool first_party(clang::SourceLocation location) const {
        if (location.isInvalid()) return false;
        const std::string file = sources_.file_of(location);
        const bool owned = !file.empty() && under_any(file, settings_.first_party_roots);
        return owned;
    }

    const Settings& settings_;
    Sources sources_;
    FunctionChecker checker_;
};

class Consumer final : public clang::ASTConsumer {
public:
    Consumer(const Settings& settings, EntrySet& entries) noexcept : settings_(settings), entries_(entries) {}

    void HandleTranslationUnit(clang::ASTContext& context) override {
        Visitor visitor(context, settings_, entries_);
        visitor.TraverseDecl(context.getTranslationUnitDecl());
    }

private:
    const Settings& settings_;
    EntrySet& entries_;
};

class Action final : public clang::ASTFrontendAction {
public:
    Action(const Settings& settings, EntrySet& entries) noexcept : settings_(settings), entries_(entries) {}

    std::unique_ptr<clang::ASTConsumer> CreateASTConsumer(clang::CompilerInstance& instance,
                                                          llvm::StringRef file) override {
        static_cast<void>(instance);
        static_cast<void>(file);
        std::unique_ptr<clang::ASTConsumer> consumer = std::make_unique<Consumer>(settings_, entries_);
        return consumer;
    }

private:
    const Settings& settings_;
    EntrySet& entries_;
};

class ActionFactory final : public clang::tooling::FrontendActionFactory {
public:
    ActionFactory(const Settings& settings, EntrySet& entries) noexcept : settings_(settings), entries_(entries) {}

    std::unique_ptr<clang::FrontendAction> create() override {
        std::unique_ptr<clang::FrontendAction> action = std::make_unique<Action>(settings_, entries_);
        return action;
    }

private:
    const Settings& settings_;
    EntrySet& entries_;
};

}  // namespace early_exit

namespace {

llvm::cl::OptionCategory tool_category("early_exit_check options");
llvm::cl::opt<std::string> settings_path("config", llvm::cl::desc("the settings file check_early_exit.py writes"),
                                         llvm::cl::Required, llvm::cl::cat(tool_category));

}  // namespace

int main(int argc, const char** argv) {
    llvm::Expected<clang::tooling::CommonOptionsParser> options =
        clang::tooling::CommonOptionsParser::create(argc, argv, tool_category);
    if (!options) {
        llvm::errs() << llvm::toString(options.takeError()) << "\n";
        return 1;
    }
    early_exit::Settings settings{};
    if (!early_exit::read_settings(settings_path, settings)) return 1;
    early_exit::EntrySet entries{};
    clang::tooling::ClangTool tool((*options).getCompilations(), (*options).getSourcePathList());
    early_exit::ActionFactory factory(settings, entries);
    const int status = tool.run(&factory);
    entries.write(llvm::outs());
    const int result = status == 0 ? 0 : 1;
    return result;
}
